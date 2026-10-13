#!/usr/bin/env python3
"""Targeted quote enrichment for underquoted key CGI articles using PostgreSQL.

Unlike batch_enrich_quotes.py (SQLite), this queries the full PostgreSQL DB
(3.87M interventions) and uses article aliases/keywords to find more quotes.

Usage:
    python scripts/enrich_key_articles_quotes.py          # Dry run
    python scripts/enrich_key_articles_quotes.py --apply   # Apply changes
"""
import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.batch_enrich_quotes import (
    PROCEDURAL_PATTERNS,
    CGI_STRICT,
    CGI_RELAXED,
    OTHER_CODES,
    build_article_pattern,
    classify_ref_ambiguity,
    deduplicate_quote,
    extract_quote,
    is_quality_quote,
)
from src.db.models import Debat, DossierLegislatif, Intervention, Loi, get_session

INDEX_PATH = Path("data/article_modifications.json")

# ── Article aliases for keyword-based search ─────────────────────────────────
# These map to well-known names/concepts that appear in debates even when the
# article number isn't explicitly mentioned.  Each alias requires CGI context
# to avoid false positives.

ARTICLE_ALIASES = {
    "150-0 B": ["sursis d'imposition"],
    "885 I bis": ["pacte Dutreil", "pacte d'actionnaires ISF"],
    "39 duodecies": ["plus-values professionnelles à court terme"],
    "44 sexies": ["exonération des entreprises nouvelles zone"],
    "44 sexies A": ["jeunes entreprises innovantes", "JEI"],
    "163 bis G": ["BSPCE", "bons de souscription de parts de créateur"],
    "80 quaterdecies": ["attributions gratuites d'actions"],
    "119 bis": ["retenue à la source sur les dividendes"],
    "182 B": ["retenue à la source sur les prestations", "retenue à la source sur les redevances"],
    "223 B": ["intégration fiscale"],
    "145": ["régime mère-fille", "régime des sociétés mères"],
    "150 VH bis": ["actifs numériques", "crypto-actifs"],
    "964": ["fortune immobilière"],
    "965": ["biens imposables IFI"],
}


def find_interventions_by_article(session, ref, aliases, limit=500):
    """Search PostgreSQL for interventions mentioning this article or its aliases."""
    results = []

    # Primary: "article {ref}" in content
    like_pattern = f"%article {ref}%"
    rows = (
        session.query(
            Intervention.orateur,
            Intervention.fonction,
            Intervention.contenu,
            Debat.chambre,
            Debat.date,
        )
        .join(Debat, Intervention.debat_id == Debat.id)
        .filter(
            Intervention.contenu.ilike(like_pattern),
            Intervention.contenu.isnot(None),
        )
        .order_by(Debat.date)
        .limit(limit)
        .all()
    )
    for orateur, fonction, contenu, chambre, date in rows:
        if contenu and len(contenu) > 80:
            results.append({
                "orateur": orateur or "",
                "fonction": fonction or "",
                "contenu": contenu,
                "chambre": chambre or "",
                "date": str(date)[:10] if date else "",
                "source": "direct",
            })

    # Alias search: require CGI context near the alias mention
    for alias in aliases:
        alias_rows = (
            session.query(
                Intervention.orateur,
                Intervention.fonction,
                Intervention.contenu,
                Debat.chambre,
                Debat.date,
            )
            .join(Debat, Intervention.debat_id == Debat.id)
            .filter(
                Intervention.contenu.ilike(f"%{alias}%"),
                Intervention.contenu.ilike("%code général des impôts%")
                | Intervention.contenu.ilike("% CGI%")
                | Intervention.contenu.ilike(f"%article {ref}%"),
            )
            .order_by(Debat.date)
            .limit(100)
            .all()
        )
        for orateur, fonction, contenu, chambre, date in alias_rows:
            if contenu and len(contenu) > 80:
                # Avoid duplicates with direct search
                if not any(r["contenu"] == contenu for r in results):
                    results.append({
                        "orateur": orateur or "",
                        "fonction": fonction or "",
                        "contenu": contenu,
                        "chambre": chambre or "",
                        "date": str(date)[:10] if date else "",
                        "source": f"alias:{alias}",
                    })

    return results


def extract_quote_flexible(contenu, ref, aliases, max_chars=400):
    """Extract quote around article mention or alias mention."""
    # Try direct article ref first
    quote = extract_quote(contenu, ref, max_chars)
    if quote:
        return quote

    # Try alias mentions
    for alias in aliases:
        match = re.search(re.escape(alias), contenu, re.IGNORECASE)
        if match:
            mention_pos = match.start()
            start = max(0, mention_pos - 200)
            for i in range(mention_pos - 1, start, -1):
                if contenu[i] in ".!\n" and i < mention_pos - 10:
                    start = i + 1
                    break
            end = min(len(contenu), mention_pos + max_chars)
            found_end = False
            for i in range(mention_pos + len(alias) + 2, end):
                if contenu[i] in ".!" and i > mention_pos + 50:
                    end = i + 1
                    found_end = True
                    break
                if contenu[i] == "\n" and i > mention_pos + 30:
                    end = i
                    found_end = True
                    break
            text = contenu[start:end].strip()
            text = re.sub(r"\s+", " ", text)
            if not found_end and len(text) >= max_chars - 20:
                text = text.rstrip() + "..."
            if text and len(text) > 60:
                return text

    return None


def main():
    parser = argparse.ArgumentParser(description="Targeted quote enrichment for key articles")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    args = parser.parse_args()

    print(f"Loading index from {INDEX_PATH}...")
    with open(INDEX_PATH) as f:
        index_data = json.load(f)
    articles = index_data["articles"]

    # Load key articles list
    with open("data/cgi_articles_reference.json") as f:
        ref_data = json.load(f)
    key_refs = {art["reference"] for art in ref_data["articles"]}

    # Find underquoted key articles
    underquoted = {}
    for ref in key_refs:
        art = articles.get(ref, {})
        cq = art.get("curated_quotes", {})
        n_quotes = sum(len(v) for v in cq.values()) if isinstance(cq, dict) else 0
        if n_quotes < 3:
            underquoted[ref] = art

    print(f"Key articles: {len(key_refs)}")
    print(f"Underquoted (<3 quotes): {len(underquoted)}")
    print()

    if not underquoted:
        print("All key articles have >= 3 quotes. Nothing to do.")
        return

    session = get_session()
    session.get_bind().echo = False

    stats = {"processed": 0, "enriched": 0, "quotes_added": 0,
             "rejected_quality": 0, "rejected_dedup": 0}

    for ref in sorted(underquoted.keys()):
        art_data = articles.get(ref, {})
        aliases = ARTICLE_ALIASES.get(ref, [])
        ambiguity = classify_ref_ambiguity(ref)
        pattern = build_article_pattern(ref)

        # Get existing quotes
        curated = art_data.get("curated_quotes", {})
        existing_flat = []
        for year_quotes in curated.values():
            existing_flat.extend(year_quotes)
        existing_count = len(existing_flat)

        # Get modification years
        mod_years = []
        for mod in art_data.get("modifications", []):
            y = mod.get("annee") or (mod.get("date", "")[:4] if mod.get("date") else None)
            if y:
                mod_years.append(int(y))
        mod_years = sorted(set(mod_years))

        # Search PostgreSQL
        interventions = find_interventions_by_article(session, ref, aliases)

        MAX_NEW = 10
        MAX_TOTAL = 15
        new_quotes_by_year = defaultdict(list)
        total_new = 0

        stats["processed"] += 1

        for row in interventions:
            if total_new >= MAX_NEW or existing_count + total_new >= MAX_TOTAL:
                break

            contenu = row["contenu"]
            orateur = row["orateur"]
            fonction = row["fonction"]
            date = row["date"]

            # For direct mentions, verify pattern match
            if row["source"] == "direct" and not pattern.search(contenu):
                continue

            # Extract quote
            quote_text = extract_quote_flexible(contenu, ref, aliases)
            if not quote_text:
                continue

            # Quality check - for key articles, we treat "high" as "moderate"
            # since we know these are real CGI articles
            effective_ambiguity = "moderate" if ambiguity == "high" else ambiguity
            if not is_quality_quote(
                quote_text, orateur, fonction, ref, effective_ambiguity,
                full_contenu=contenu,
            ):
                stats["rejected_quality"] += 1
                continue

            # Year assignment
            if mod_years:
                from scripts.batch_enrich_quotes import assign_year
                year = assign_year(date, mod_years)
            else:
                year = date[:4] if date else None
            if not year:
                continue

            # Dedup
            all_existing = existing_flat + [
                q for qs in new_quotes_by_year.values() for q in qs
            ]
            if deduplicate_quote(quote_text, all_existing):
                stats["rejected_dedup"] += 1
                continue

            # Clean speaker
            clean_orateur = re.sub(r"^(M\.|Mme)\s+le\s+président\.\s*", "", orateur).strip()
            if not clean_orateur:
                clean_orateur = orateur

            new_quotes_by_year[year].append({
                "citation": quote_text,
                "orateur": clean_orateur,
                "fonction": fonction,
                "date": date,
                "source": row["source"],
            })
            total_new += 1

        if total_new > 0:
            stats["enriched"] += 1
            stats["quotes_added"] += total_new
            final_total = existing_count + total_new
            print(f"  {ref:30s}  +{total_new} quotes ({existing_count} → {final_total})")
            for year, quotes in sorted(new_quotes_by_year.items()):
                for q in quotes:
                    print(f"    [{year}] {q['orateur'][:30]:30s} | {q['citation'][:80]}...")

            if args.apply:
                if "curated_quotes" not in art_data:
                    art_data["curated_quotes"] = {}
                for year, quotes in new_quotes_by_year.items():
                    if year not in art_data["curated_quotes"]:
                        art_data["curated_quotes"][year] = []
                    art_data["curated_quotes"][year].extend(quotes)
        else:
            print(f"  {ref:30s}  no new quotes found (rej quality={stats['rejected_quality']}, dedup={stats['rejected_dedup']})")

    session.close()

    print(f"\n{'='*60}")
    print(f"RESULTS")
    print(f"{'='*60}")
    print(f"  Articles processed:   {stats['processed']}")
    print(f"  Articles enriched:    {stats['enriched']}")
    print(f"  Quotes added:         {stats['quotes_added']}")
    print(f"  Rejected (quality):   {stats['rejected_quality']}")
    print(f"  Rejected (dedup):     {stats['rejected_dedup']}")

    if not args.apply:
        print(f"\nDRY RUN: No changes saved. Use --apply to save.")
        return

    # Save
    backup = INDEX_PATH.with_suffix(".json.bak")
    shutil.copy2(INDEX_PATH, backup)
    print(f"  Backup: {backup}")

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)
    print(f"  Saved to {INDEX_PATH}")


if __name__ == "__main__":
    main()
