#!/usr/bin/env python3
"""Import archive OCR samples into PostgreSQL and compare quality with post-2002 data.

Parses 22 OCR text files, imports as orphan debates, then computes side-by-side
quality metrics against the post-2002 structured data baseline.

Usage:
    python scripts/ocr_pipeline/import_and_compare_samples.py
    python scripts/ocr_pipeline/import_and_compare_samples.py --skip-import
    python scripts/ocr_pipeline/import_and_compare_samples.py --no-purge
"""

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.db.models import Debat, Intervention
from src.parsers.archive_debates import parse_archive_debate_from_text

logger = logging.getLogger(__name__)

DB_URL = "postgresql://localhost/legislateur_local"
OCR_TEXT_DIR = Path("data/ocr_pipeline/validation_results/ocr_text")
BASELINE_FILE = Path("data/ocr_pipeline/post2002_baseline.json")

FILENAME_RE = re.compile(r"leg(\d+)_(.+?)_(\d+)\.pdf\.txt")

LEGISLATURES = {
    1: "1958-1962", 2: "1962-1967", 3: "1967-1968", 4: "1968-1973",
    5: "1973-1978", 6: "1978-1981", 7: "1981-1986", 8: "1986-1988",
    9: "1988-1993", 10: "1993-1997", 11: "1997-2002",
}

# Speaker name format patterns (same as extract_post2002_baseline.py)
_NAMED_RE = re.compile(r"^M(?:me|lle)?\.?\s+[A-ZÀ-Ý][a-zà-ÿ]")
_ROLE_RE = re.compile(r"^M(?:me)?\.?\s+l[ea]\s+")

# OCR artifact pattern (non-French chars)
_ARTIFACT_RE = re.compile(r"[^\x00-\x7Fà-ÿÀ-Ý\s°ªº«»–—''""…€œŒæÆ]")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_filename(filename: str) -> dict | None:
    """Extract legislature, session_name, page from OCR text filename."""
    m = FILENAME_RE.match(filename)
    if not m:
        return None
    return {
        "legislature": int(m.group(1)),
        "session_name": m.group(2),
        "page": m.group(3),
        "chambre": "AN",
    }


def parse_ocr_files(ocr_dir: Path, verbose: bool = False):
    """Parse all OCR text files into ParsedDebat objects."""
    results = []
    failed = []

    for txt_file in sorted(ocr_dir.glob("*.pdf.txt")):
        meta = parse_filename(txt_file.name)
        if not meta:
            failed.append((txt_file.name, "filename parse error"))
            continue

        raw_text = txt_file.read_text(encoding="utf-8")
        parsed = parse_archive_debate_from_text(
            raw_text,
            chambre=meta["chambre"],
            session_name=meta["session_name"],
            filename=meta["page"],
        )

        if parsed is None:
            failed.append((txt_file.name, "parser returned None"))
            continue

        if verbose:
            print(f"  {txt_file.name}: {len(parsed.interventions)} interventions, "
                  f"date={parsed.date.strftime('%Y-%m-%d') if parsed.date else 'None'}")

        results.append((meta, parsed))

    return results, failed


# ---------------------------------------------------------------------------
# DB import
# ---------------------------------------------------------------------------

def _get_local_session():
    """Create a session connected to the local PostgreSQL."""
    engine = create_engine(
        DB_URL,
        connect_args={"options": "-c statement_timeout=120000"},  # 120s timeout
    )
    Session = sessionmaker(bind=engine)
    return Session()


def _terminate_stale_connections(session):
    """Kill any stale connections holding locks on our tables."""
    session.execute(text(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "AND state = 'idle in transaction' "
        "AND query LIKE '%archive%'"
    ))


def purge_archive_imports(session) -> int:
    """Delete all archive OCR debates from DB."""
    # Kill stale connections that might hold locks
    _terminate_stale_connections(session)
    session.commit()

    # Get explicit IDs first (fast index scan on reference_externe)
    ids = [
        r[0] for r in session.execute(
            text("SELECT id FROM debats WHERE reference_externe LIKE 'archive_an:%'")
        ).fetchall()
    ]
    if not ids:
        return 0
    # Delete by explicit ID list (uses PK index, avoids full table scan)
    id_list = ",".join(str(i) for i in ids)
    # Delete chunk_embeddings first (FK on intervention_id has no index → full scan otherwise)
    session.execute(text(
        f"DELETE FROM chunk_embeddings WHERE intervention_id IN "
        f"(SELECT id FROM interventions WHERE debat_id IN ({id_list}))"
    ))
    session.execute(text(f"DELETE FROM interventions WHERE debat_id IN ({id_list})"))
    session.execute(text(f"DELETE FROM debats WHERE id IN ({id_list})"))
    return len(ids)


def _import_orphan_debat(session, debat) -> Debat:
    """Import a ParsedDebat as orphan (no dossier). Idempotent via reference_externe."""
    if debat.reference:
        existing = session.query(Debat).filter_by(
            reference_externe=debat.reference
        ).first()
        if existing:
            return existing

    db_debat = Debat(
        dossier_id=None,
        chambre=debat.chambre,
        date=debat.date,
        etape=debat.etape,
        contenu_integral=debat.contenu_integral,
        url_source=debat.url_source,
        reference_externe=debat.reference,
    )
    session.add(db_debat)
    session.flush()

    for interv in debat.interventions:
        db_interv = Intervention(
            debat_id=db_debat.id,
            orateur=interv.orateur,
            fonction=interv.fonction,
            contenu=interv.contenu,
            articles_mentionnes=interv.articles_mentionnes,
            timestamp_debut=interv.timestamp,
            ordre=interv.ordre,
        )
        session.add(db_interv)

    session.flush()
    return db_debat


def import_to_db(parsed_list: list, purge: bool = True) -> dict:
    """Import parsed debates into local PostgreSQL."""
    session = _get_local_session()
    stats = {"imported": 0, "skipped": 0, "failed": 0, "purged": 0}

    try:
        if purge:
            stats["purged"] = purge_archive_imports(session)
            session.commit()
            if stats["purged"]:
                print(f"  Purged {stats['purged']} existing archive debates")

        for i, (meta, parsed) in enumerate(parsed_list):
            try:
                db_debat = _import_orphan_debat(session, parsed)
                if db_debat.id:
                    stats["imported"] += 1
                else:
                    stats["skipped"] += 1

                if (i + 1) % 10 == 0:
                    session.commit()
            except Exception as e:
                session.rollback()
                stats["failed"] += 1
                logger.warning(f"Failed to import {meta['session_name']}/{meta['page']}: {e}")

        session.commit()
    finally:
        session.close()

    return stats


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------

def query_archive_population() -> dict:
    """Query archive debates and interventions from DB."""
    engine = create_engine(DB_URL)

    with engine.connect() as conn:
        # Debate-level
        debates = conn.execute(text("""
            SELECT d.id, d.chambre, d.date, d.etape, d.reference_externe,
                   d.dossier_id, d.contenu_integral,
                   COUNT(i.id) AS intervention_count
            FROM debats d
            LEFT JOIN interventions i ON i.debat_id = d.id
            WHERE d.reference_externe LIKE 'archive_an:%'
            GROUP BY d.id
            ORDER BY d.date
        """)).fetchall()

        # Intervention-level
        interventions = conn.execute(text("""
            SELECT i.id, i.orateur, i.fonction, i.contenu,
                   i.articles_mentionnes, i.ordre,
                   d.reference_externe, d.date AS debate_date
            FROM interventions i
            JOIN debats d ON i.debat_id = d.id
            WHERE d.reference_externe LIKE 'archive_an:%'
            ORDER BY d.date, i.ordre
        """)).fetchall()

    return {"debates": debates, "interventions": interventions}


def load_post2002_baseline() -> dict:
    """Load pre-computed post-2002 baseline from JSON."""
    with open(BASELINE_FILE, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def percentiles(values: list, pcts: list[int] = None) -> dict:
    """Compute percentile statistics."""
    if pcts is None:
        pcts = [10, 25, 50, 75, 90]
    if not values:
        return {"count": 0, "mean": 0, **{f"p{p}": 0 for p in pcts}}
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


def compute_archive_metrics(data: dict) -> dict:
    """Compute quality metrics from archive DB data."""
    debates = data["debates"]
    interventions = data["interventions"]

    total_debates = len(debates)
    total_interventions = len(interventions)
    if total_interventions == 0:
        return {"error": "No interventions found"}

    # Debate-level
    intervention_counts = [r.intervention_count for r in debates]
    has_date = sum(1 for r in debates if r.date is not None)
    has_ref = sum(1 for r in debates if r.reference_externe)
    has_dossier = sum(1 for r in debates if r.dossier_id is not None)
    has_contenu_integral = sum(
        1 for r in debates if r.contenu_integral and len(r.contenu_integral) > 0
    )

    # Intervention-level
    contenu_lengths = [len(r.contenu) for r in interventions if r.contenu]
    orateur_lengths = [len(r.orateur) for r in interventions if r.orateur]

    # Speaker format
    named_count = sum(1 for r in interventions if r.orateur and _NAMED_RE.match(r.orateur))
    role_count = sum(1 for r in interventions if r.orateur and _ROLE_RE.match(r.orateur))
    valid_format = named_count + role_count

    # Speaker leak (orateur too long or contains sentence-like text)
    leak_count = sum(
        1 for r in interventions
        if r.orateur and (len(r.orateur) > 80 or "," in r.orateur[20:])
    )

    # Function detection
    fonction_counts = Counter(r.fonction for r in interventions)
    non_autre_count = sum(
        1 for r in interventions
        if r.fonction and r.fonction not in ("autre", None)
    )

    # Article mentions
    has_articles = sum(
        1 for r in interventions
        if r.articles_mentionnes and len(r.articles_mentionnes) > 0
    )

    # Content coherence (artifact detection)
    artifact_count = 0
    for r in interventions:
        if r.contenu:
            sample = r.contenu[:200]
            if _ARTIFACT_RE.search(sample):
                artifact_count += 1
    coherent_count = total_interventions - artifact_count

    # Per-legislature breakdown
    leg_data = {}
    for r in debates:
        ref = r.reference_externe or ""
        # Extract legislature from reference: archive_an:session/page
        session_part = ref.replace("archive_an:", "").split("/")[0]
        leg_match = None
        for txt_file in OCR_TEXT_DIR.glob("*.pdf.txt"):
            meta = parse_filename(txt_file.name)
            if meta and meta["session_name"] == session_part:
                leg_match = meta["legislature"]
                break
        if leg_match is None:
            leg_match = 0

        if leg_match not in leg_data:
            leg_data[leg_match] = {"debates": 0, "interventions": 0, "refs": []}
        leg_data[leg_match]["debates"] += 1
        leg_data[leg_match]["interventions"] += r.intervention_count
        leg_data[leg_match]["refs"].append(ref)

    # Per-legislature intervention metrics
    for leg_num in leg_data:
        leg_refs = set(leg_data[leg_num]["refs"])
        leg_intervs = [r for r in interventions if r.reference_externe in leg_refs]
        n = len(leg_intervs)
        if n == 0:
            continue
        leg_named = sum(1 for r in leg_intervs if r.orateur and _NAMED_RE.match(r.orateur))
        leg_role = sum(1 for r in leg_intervs if r.orateur and _ROLE_RE.match(r.orateur))
        leg_non_autre = sum(
            1 for r in leg_intervs if r.fonction and r.fonction not in ("autre", None)
        )
        leg_coherent = sum(
            1 for r in leg_intervs
            if not r.contenu or not _ARTIFACT_RE.search(r.contenu[:200])
        )
        leg_data[leg_num]["speaker_format_pct"] = round(100.0 * (leg_named + leg_role) / n, 1)
        leg_data[leg_num]["function_detect_pct"] = round(100.0 * leg_non_autre / n, 1)
        leg_data[leg_num]["coherence_pct"] = round(100.0 * leg_coherent / n, 1)

    return {
        "debate_stats": {
            "total": total_debates,
            "has_date_pct": round(100.0 * has_date / total_debates, 1),
            "has_reference_externe_pct": round(100.0 * has_ref / total_debates, 1),
            "linked_to_dossier_pct": round(100.0 * has_dossier / total_debates, 1),
            "has_contenu_integral_pct": round(100.0 * has_contenu_integral / total_debates, 1),
            "interventions_per_debate": percentiles(intervention_counts),
        },
        "intervention_stats": {
            "total": total_interventions,
            "contenu_length": percentiles(contenu_lengths),
            "orateur_length": percentiles(orateur_lengths),
            "speaker_name_format": {
                "named_pct": round(100.0 * named_count / total_interventions, 1),
                "role_based_pct": round(100.0 * role_count / total_interventions, 1),
                "valid_total_pct": round(100.0 * valid_format / total_interventions, 1),
            },
            "speaker_no_leak_pct": round(100.0 * (total_interventions - leak_count) / total_interventions, 1),
            "fonction_distribution": {
                k: {"count": v, "pct": round(100.0 * v / total_interventions, 1)}
                for k, v in sorted(fonction_counts.items(), key=lambda x: -x[1])
            },
            "function_detect_pct": round(100.0 * non_autre_count / total_interventions, 1),
            "has_articles_mentionnes_pct": round(100.0 * has_articles / total_interventions, 1),
            "content_coherence_pct": round(100.0 * coherent_count / total_interventions, 1),
        },
        "per_legislature": leg_data,
    }


def detect_quality_issues(interventions) -> list[dict]:
    """Identify specific problematic interventions."""
    issues = []

    for r in interventions:
        ref = r.reference_externe or "unknown"

        # Artifact in content
        if r.contenu and _ARTIFACT_RE.search(r.contenu[:200]):
            issues.append({
                "type": "artifact",
                "ref": ref,
                "orateur": r.orateur,
                "preview": r.contenu[:150].replace("\n", " "),
            })

        # Malformed speaker name
        if r.orateur and not _NAMED_RE.match(r.orateur) and not _ROLE_RE.match(r.orateur):
            issues.append({
                "type": "malformed_speaker",
                "ref": ref,
                "orateur": r.orateur,
                "preview": (r.contenu or "")[:100].replace("\n", " "),
            })

        # Speaker leak
        if r.orateur and (len(r.orateur) > 80 or "," in r.orateur[20:]):
            issues.append({
                "type": "speaker_leak",
                "ref": ref,
                "orateur": r.orateur[:100],
                "preview": (r.contenu or "")[:100].replace("\n", " "),
            })

        # Too short
        if r.contenu and len(r.contenu) < 20:
            issues.append({
                "type": "too_short",
                "ref": ref,
                "orateur": r.orateur,
                "preview": r.contenu.replace("\n", " "),
            })

        # Too long
        if r.contenu and len(r.contenu) > 10000:
            issues.append({
                "type": "too_long",
                "ref": ref,
                "orateur": r.orateur,
                "preview": r.contenu[:150].replace("\n", " "),
                "length": len(r.contenu),
            })

    return issues


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_comparison_report(archive: dict, baseline: dict, issues: list[dict]):
    """Print side-by-side comparison report."""
    a_ds = archive["debate_stats"]
    a_is = archive["intervention_stats"]
    b_ds = baseline["debate_stats"]
    b_is = baseline["intervention_stats"]

    print("\n" + "=" * 72)
    print("  ARCHIVE OCR SAMPLES vs POST-2002 BASELINE")
    print("=" * 72)

    # Import summary
    print(f"\n## Import Summary")
    print(f"  Archive debates: {a_ds['total']}")
    print(f"  Archive interventions: {a_is['total']}")
    print(f"  Post-2002 debates: {b_ds['total']}")
    print(f"  Post-2002 interventions: {b_is['total']}")

    # Side-by-side metrics
    print(f"\n## Quality Metrics (side-by-side)")
    print(f"{'Metric':<35} {'Archive':>12} {'Post-2002':>12} {'Status':>8}")
    print("-" * 72)

    def row(label, arch_val, base_val, status):
        print(f"  {label:<33} {arch_val:>12} {base_val:>12} {status:>8}")

    # Contenu length
    a_cl = a_is["contenu_length"]
    b_cl = b_is["contenu_length"]
    row("Contenu length (median)", f"{a_cl['p50']}", f"{b_cl['p50']}", "OK" if a_cl["p50"] > 50 else "LOW")
    row("Contenu length (mean)", f"{a_cl['mean']}", f"{b_cl['mean']}", "OK" if a_cl["mean"] > 200 else "LOW")
    row("Contenu length (p10)", f"{a_cl['p10']}", f"{b_cl['p10']}", "OK" if a_cl["p10"] > 15 else "LOW")
    row("Contenu length (p90)", f"{a_cl['p90']}", f"{b_cl['p90']}", "OK")

    # Orateur length
    a_ol = a_is["orateur_length"]
    b_ol = b_is["orateur_length"]
    row("Orateur length (median)", f"{a_ol['p50']}", f"{b_ol['p50']}", "OK")

    # Speaker format
    a_snf = a_is["speaker_name_format"]
    b_snf = b_is["speaker_name_format"]
    a_valid = a_snf["valid_total_pct"]
    b_valid = round(b_snf["named_pct"] + b_snf["role_based_pct"], 1)
    status = "OK" if a_valid >= 90 else "LOW"
    row("Speaker valid format %", f"{a_valid}%", f"{b_valid}%", status)
    row("  Named (M. Dupont)", f"{a_snf['named_pct']}%", f"{b_snf['named_pct']}%", "")
    row("  Role-based (M. le pres.)", f"{a_snf['role_based_pct']}%", f"{b_snf['role_based_pct']}%", "")

    # Speaker no-leak
    row("Speaker no-leak %", f"{a_is['speaker_no_leak_pct']}%", "n/a", "OK" if a_is["speaker_no_leak_pct"] >= 95 else "LOW")

    # Function detection
    row("Function detection %", f"{a_is['function_detect_pct']}%", "n/a*", "OK" if a_is["function_detect_pct"] >= 30 else "LOW")

    # Article mentions
    row("Article mention %", f"{a_is['has_articles_mentionnes_pct']}%", f"{b_is['has_articles_mentionnes_pct']}%",
        "OK" if a_is["has_articles_mentionnes_pct"] >= 5 else "LOW")

    # Content coherence
    row("Content coherence %", f"{a_is['content_coherence_pct']}%", "n/a", "OK" if a_is["content_coherence_pct"] >= 90 else "LOW")

    # Has contenu_integral
    row("Has contenu_integral %", f"{a_ds['has_contenu_integral_pct']}%", "100.0%",
        "OK" if a_ds["has_contenu_integral_pct"] >= 90 else "LOW")

    print()
    print("  * Post-2002 fonction has encoding issues (HTML entities) — not directly comparable")

    # Structural metrics
    print(f"\n## Structural Metrics (expected differences)")
    print(f"{'Metric':<35} {'Archive':>12} {'Post-2002':>12} {'Note':>12}")
    print("-" * 72)
    a_ipd = a_ds["interventions_per_debate"]
    b_ipd = b_ds["interventions_per_debate"]
    row("Interventions/debate (mean)", f"{a_ipd['mean']}", f"{b_ipd['mean']}", "EXPECTED")
    row("Interventions/debate (median)", f"{a_ipd['p50']}", f"{b_ipd['p50']}", "EXPECTED")
    row("Date extraction %", f"{a_ds['has_date_pct']}%", "100.0%", "OK" if a_ds["has_date_pct"] >= 95 else "LOW")
    row("Has reference_externe %", f"{a_ds['has_reference_externe_pct']}%", f"{b_ds['has_reference_externe_pct']}%", "OK")
    row("Linked to dossier %", f"{a_ds['linked_to_dossier_pct']}%", f"{b_ds['linked_to_dossier_pct']}%", "EXPECTED")

    # Fonction distribution
    print(f"\n## Fonction Distribution")
    print(f"  {'Fonction':<25} {'Archive':>12} {'Post-2002':>12}")
    print("  " + "-" * 50)
    b_fonctions = b_is.get("fonction_distribution", {})
    all_fonctions = set(a_is["intervention_stats"]["fonction_distribution"].keys()) if "intervention_stats" in a_is else set()
    a_fonctions = a_is.get("fonction_distribution", {})
    for func in ["president", "ministre", "rapporteur", "autre", None]:
        func_key = func if func else "null"
        a_val = a_fonctions.get(func_key, a_fonctions.get(func, {}))
        b_val = b_fonctions.get(func_key, b_fonctions.get(func, {}))
        a_pct = a_val.get("pct", 0) if isinstance(a_val, dict) else 0
        b_pct = b_val.get("pct", 0) if isinstance(b_val, dict) else 0
        print(f"  {func_key:<25} {a_pct:>11.1f}% {b_pct:>11.1f}%")

    # Per-legislature breakdown
    print(f"\n## Per-Legislature Breakdown")
    print(f"  {'Leg':<4} {'Period':<12} {'Deb':>4} {'Intv/deb':>9} {'Spk fmt%':>9} {'Func%':>7} {'Coher%':>7}")
    print("  " + "-" * 56)
    per_leg = archive.get("per_legislature", {})
    for leg_num in sorted(per_leg.keys()):
        if leg_num == 0:
            continue
        ld = per_leg[leg_num]
        period = LEGISLATURES.get(leg_num, "?")
        n_deb = ld["debates"]
        intv_per_deb = round(ld["interventions"] / n_deb, 1) if n_deb else 0
        spk = ld.get("speaker_format_pct", 0)
        func = ld.get("function_detect_pct", 0)
        coh = ld.get("coherence_pct", 0)
        print(f"  {leg_num:<4} {period:<12} {n_deb:>4} {intv_per_deb:>9.1f} {spk:>8.1f}% {func:>6.1f}% {coh:>6.1f}%")

    # Quality issues
    print(f"\n## Quality Issues ({len(issues)} found)")
    if issues:
        issue_types = Counter(iss["type"] for iss in issues)
        print(f"  Summary: {dict(issue_types)}")

        for issue_type in ["artifact", "malformed_speaker", "speaker_leak", "too_short", "too_long"]:
            typed = [iss for iss in issues if iss["type"] == issue_type]
            if not typed:
                continue
            print(f"\n  [{issue_type}] ({len(typed)} found, showing up to 3):")
            for iss in typed[:3]:
                extra = f" ({iss['length']} chars)" if "length" in iss else ""
                print(f"    {iss['ref']} — {iss['orateur']}{extra}")
                print(f"    > {iss['preview'][:120]}")
    else:
        print("  None found!")

    print("\n" + "=" * 72)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Import archive OCR samples and compare with post-2002")
    parser.add_argument("--skip-import", action="store_true", help="Skip parse+import, only compare")
    parser.add_argument("--no-purge", action="store_true", help="Don't delete existing archive imports")
    parser.add_argument("--verbose", action="store_true", help="Show per-file parse details")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    # 1. Parse & Import
    if not args.skip_import:
        print("=" * 72)
        print("Phase 1: Parse OCR text files")
        print("=" * 72)

        if not OCR_TEXT_DIR.exists():
            print(f"ERROR: OCR text directory not found: {OCR_TEXT_DIR}")
            sys.exit(1)

        parsed_list, failed = parse_ocr_files(OCR_TEXT_DIR, verbose=args.verbose)
        total_interventions = sum(len(p.interventions) for _, p in parsed_list)
        print(f"  Parsed: {len(parsed_list)} files, {total_interventions} interventions")
        if failed:
            print(f"  Failed: {len(failed)}")
            for name, reason in failed:
                print(f"    {name}: {reason}")

        print(f"\n{'=' * 72}")
        print("Phase 2: Import to PostgreSQL")
        print("=" * 72)

        purge = not args.no_purge
        import_stats = import_to_db(parsed_list, purge=purge)
        print(f"  Imported: {import_stats['imported']}")
        if import_stats["skipped"]:
            print(f"  Skipped (already exist): {import_stats['skipped']}")
        if import_stats["failed"]:
            print(f"  Failed: {import_stats['failed']}")

    # 2. Query & Compare
    print(f"\n{'=' * 72}")
    print("Phase 3: Query and compare")
    print("=" * 72)

    print("  Querying archive population from DB...")
    archive_data = query_archive_population()
    print(f"  Found {len(archive_data['debates'])} debates, {len(archive_data['interventions'])} interventions")

    if not archive_data["debates"]:
        print("ERROR: No archive debates found in DB. Run without --skip-import first.")
        sys.exit(1)

    print("  Loading post-2002 baseline...")
    baseline = load_post2002_baseline()

    print("  Computing archive metrics...")
    archive_metrics = compute_archive_metrics(archive_data)

    print("  Detecting quality issues...")
    issues = detect_quality_issues(archive_data["interventions"])

    # 3. Report
    print_comparison_report(archive_metrics, baseline, issues)

    # 4. Save JSON
    output_file = Path("data/ocr_pipeline") / f"archive_comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now().isoformat(),
        "archive_metrics": {
            "debate_stats": archive_metrics["debate_stats"],
            "intervention_stats": archive_metrics["intervention_stats"],
        },
        "issue_count": len(issues),
        "issue_summary": dict(Counter(iss["type"] for iss in issues)),
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nJSON report saved to {output_file}")


if __name__ == "__main__":
    main()
