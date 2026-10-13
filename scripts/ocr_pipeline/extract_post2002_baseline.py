#!/usr/bin/env python3
"""Step 1: Extract post-2002 data quality baseline from local PostgreSQL.

Connects to postgresql://localhost/legislateur_local and computes reference
statistics from the structured post-2002 data (debates, interventions).
These statistics serve as the quality target for OCR'd 1958-2002 data.

Usage:
    python scripts/ocr_pipeline/extract_post2002_baseline.py
"""

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine, text

DB_URL = "postgresql://localhost/legislateur_local"
OUTPUT_DIR = Path("data/ocr_pipeline")
OUTPUT_FILE = OUTPUT_DIR / "post2002_baseline.json"


def percentiles(values: list, pcts: list[int] = [10, 25, 50, 75, 90]) -> dict:
    """Compute percentile statistics for a list of numeric values."""
    if not values:
        return {f"p{p}": 0 for p in pcts}
    arr = np.array(values)
    result = {
        "count": len(values),
        "mean": round(float(np.mean(arr)), 1),
        "min": int(np.min(arr)),
        "max": int(np.max(arr)),
    }
    for p in pcts:
        result[f"p{p}"] = round(float(np.percentile(arr, p)), 1)
    return result


def extract_baseline():
    engine = create_engine(DB_URL)

    with engine.connect() as conn:
        # --- Debate-level stats ---
        print("Querying debates (post-2002)...")
        debates = conn.execute(text("""
            SELECT d.id, d.chambre, d.date, d.etape, d.dossier_id, d.reference_externe,
                   COUNT(i.id) AS intervention_count
            FROM debats d
            LEFT JOIN interventions i ON i.debat_id = d.id
            WHERE d.date >= '2003-01-01'
            GROUP BY d.id
        """)).fetchall()

        total_debates = len(debates)
        if total_debates == 0:
            print("ERROR: No post-2002 debates found in database.")
            sys.exit(1)

        print(f"  Found {total_debates} debates")

        intervention_counts = [r.intervention_count for r in debates]
        chambre_counts = Counter(r.chambre for r in debates)
        etape_counts = Counter(r.etape for r in debates if r.etape)
        linked_count = sum(1 for r in debates if r.dossier_id is not None)
        has_reference = sum(1 for r in debates if r.reference_externe)

        debate_stats = {
            "total": total_debates,
            "by_chambre": dict(chambre_counts),
            "by_etape": dict(etape_counts),
            "linked_to_dossier_pct": round(100.0 * linked_count / total_debates, 1),
            "has_reference_externe_pct": round(100.0 * has_reference / total_debates, 1),
            "interventions_per_debate": percentiles(intervention_counts),
        }

        # --- Intervention-level stats ---
        print("Querying interventions (post-2002)...")
        interventions = conn.execute(text("""
            SELECT i.orateur, i.fonction, i.contenu, i.articles_mentionnes, i.ordre
            FROM interventions i
            JOIN debats d ON i.debat_id = d.id
            WHERE d.date >= '2003-01-01'
        """)).fetchall()

        total_interventions = len(interventions)
        print(f"  Found {total_interventions} interventions")

        contenu_lengths = [len(r.contenu) for r in interventions if r.contenu]
        orateur_lengths = [len(r.orateur) for r in interventions if r.orateur]
        fonction_counts = Counter(r.fonction for r in interventions)
        has_articles = sum(
            1 for r in interventions
            if r.articles_mentionnes and len(r.articles_mentionnes) > 0
        )

        # Speaker name format analysis
        import re
        name_pattern_named = re.compile(r"^M(?:me|lle)?\.?\s+[A-ZÀ-Ý][a-zà-ÿ]")
        name_pattern_role = re.compile(r"^M(?:me)?\.?\s+l[ea]\s+")
        named_count = sum(
            1 for r in interventions
            if r.orateur and name_pattern_named.match(r.orateur)
        )
        role_count = sum(
            1 for r in interventions
            if r.orateur and name_pattern_role.match(r.orateur)
        )

        intervention_stats = {
            "total": total_interventions,
            "contenu_length": percentiles(contenu_lengths),
            "orateur_length": percentiles(orateur_lengths),
            "fonction_distribution": {
                k: {"count": v, "pct": round(100.0 * v / total_interventions, 1)}
                for k, v in sorted(fonction_counts.items(), key=lambda x: -x[1])
            },
            "has_articles_mentionnes_pct": round(
                100.0 * has_articles / total_interventions, 1
            ),
            "speaker_name_format": {
                "named_pct": round(100.0 * named_count / total_interventions, 1),
                "role_based_pct": round(100.0 * role_count / total_interventions, 1),
            },
        }

        # --- Sample interventions ---
        print("Fetching sample interventions...")
        sample_interventions = []
        try:
            samples = conn.execute(text("""
                SELECT i.orateur, i.fonction, LEFT(i.contenu, 200) AS contenu_preview,
                       i.articles_mentionnes, d.chambre, d.date
                FROM interventions i
                JOIN debats d ON i.debat_id = d.id
                WHERE d.date >= '2010-01-01'
                  AND i.contenu IS NOT NULL
                  AND LENGTH(i.contenu) > 100
                ORDER BY RANDOM()
                LIMIT 10
            """)).fetchall()

            for s in samples:
                sample_interventions.append({
                    "orateur": s.orateur,
                    "fonction": s.fonction,
                    "contenu_preview": s.contenu_preview,
                    "articles_mentionnes": s.articles_mentionnes,
                    "chambre": s.chambre,
                    "date": s.date.isoformat() if s.date else None,
                })
        except Exception as e:
            print(f"  Warning: sample query failed (encoding issue): {e}")
            conn.rollback()

        # --- Year distribution ---
        print("Computing year distribution...")
        year_dist = conn.execute(text("""
            SELECT EXTRACT(YEAR FROM date)::int AS year, chambre,
                   COUNT(*) AS debate_count,
                   SUM(intervention_count) AS total_interventions
            FROM (
                SELECT d.id, d.date, d.chambre, COUNT(i.id) AS intervention_count
                FROM debats d
                LEFT JOIN interventions i ON i.debat_id = d.id
                WHERE d.date >= '2003-01-01'
                GROUP BY d.id
            ) sub
            GROUP BY year, chambre
            ORDER BY year, chambre
        """)).fetchall()

        year_distribution = {}
        for r in year_dist:
            key = str(r.year)
            if key not in year_distribution:
                year_distribution[key] = {}
            year_distribution[key][r.chambre] = {
                "debates": r.debate_count,
                "interventions": int(r.total_interventions) if r.total_interventions else 0,
            }

    # --- Build result ---
    baseline = {
        "generated_at": datetime.now().isoformat(),
        "database": DB_URL,
        "filter": "date >= 2003-01-01",
        "debate_stats": debate_stats,
        "intervention_stats": intervention_stats,
        "year_distribution": year_distribution,
        "sample_interventions": sample_interventions,
    }

    # --- Save ---
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)
    print(f"\nBaseline saved to {OUTPUT_FILE}")

    # --- Print report ---
    print_report(baseline)

    return baseline


def print_report(b: dict):
    ds = b["debate_stats"]
    ist = b["intervention_stats"]

    print("\n" + "=" * 70)
    print("POST-2002 DATA QUALITY BASELINE")
    print("=" * 70)

    print(f"\n## Debates: {ds['total']}")
    print(f"  By chambre: {ds['by_chambre']}")
    print(f"  By etape: {ds['by_etape']}")
    print(f"  Linked to dossier: {ds['linked_to_dossier_pct']}%")
    print(f"  Has reference_externe: {ds['has_reference_externe_pct']}%")

    ipd = ds["interventions_per_debate"]
    print(f"\n## Interventions per debate:")
    print(f"  Mean: {ipd['mean']}, Median: {ipd['p50']}")
    print(f"  P10: {ipd['p10']}, P90: {ipd['p90']}")
    print(f"  Min: {ipd['min']}, Max: {ipd['max']}")

    print(f"\n## Interventions: {ist['total']}")
    cl = ist["contenu_length"]
    print(f"  Contenu length - Mean: {cl['mean']}, Median: {cl['p50']}")
    print(f"  P10: {cl['p10']}, P90: {cl['p90']}")

    ol = ist["orateur_length"]
    print(f"  Orateur length - Mean: {ol['mean']}, Max: {ol['max']}")

    print(f"\n## Fonction distribution:")
    for func, data in ist["fonction_distribution"].items():
        print(f"  {func or 'NULL'}: {data['count']} ({data['pct']}%)")

    print(f"\n## Article mentions: {ist['has_articles_mentionnes_pct']}%")

    snf = ist["speaker_name_format"]
    print(f"## Speaker name format:")
    print(f"  Named (M. Dupont): {snf['named_pct']}%")
    print(f"  Role-based (M. le président): {snf['role_based_pct']}%")

    print(f"\n## Sample interventions:")
    for i, s in enumerate(b["sample_interventions"], 1):
        print(f"\n  [{i}] {s['orateur']} ({s['fonction']}) - {s['chambre']} {s['date']}")
        print(f"      {s['contenu_preview'][:120]}...")
        if s["articles_mentionnes"]:
            print(f"      Articles: {s['articles_mentionnes']}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    extract_baseline()
