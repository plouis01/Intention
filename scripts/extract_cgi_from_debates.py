#!/usr/bin/env python3
"""Extract CGI article modifications from pre-2002 parliamentary debate interventions.

Since pre-2002 law article texts are often stubs ("a modifié les dispositions suivantes"),
this script extracts CGI article references from the much richer debate text instead.

Each intervention mentioning the CGI is scanned with the CGIExtractor regex patterns.
The results are grouped by CGI article and merged into article_modifications.json.

Usage:
    python scripts/extract_cgi_from_debates.py --dry-run
    python scripts/extract_cgi_from_debates.py
"""

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text

from src.analysis.cgi_extractor import CGIExtractor, ExtractedModification
from src.db.models import get_engine, get_session

from scripts.generate_article_index import (
    build_article_index,
    enrich_from_reference,
    enrich_law_titles,
    generate_article_keywords,
    load_law_titles,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

INDEX_PATH = project_root / "data" / "article_modifications.json"
REFERENCE_PATH = project_root / "data" / "cgi_articles_reference.json"
CUTOFF_DATE = "2002-01-01"

CGI_FILTER = """
    (i.contenu ILIKE '%%code général des impôts%%'
     OR i.contenu ILIKE '%%code general des impots%%'
     OR i.contenu ILIKE '%%du CGI%%'
     OR i.contenu ILIKE '%%au CGI%%'
     OR i.contenu ILIKE '%%le CGI%%')
"""

QUERY = f"""
    SELECT i.contenu, l.numero, l.type, l.annee, l.date_promulgation, d.date
    FROM interventions i
    JOIN debats d ON i.debat_id = d.id
    JOIN dossiers_legislatifs dl ON d.dossier_id = dl.id
    JOIN lois l ON dl.loi_id = l.id
    WHERE d.date < :cutoff
    AND {CGI_FILTER}
"""


CGI_CONTEXT_RE = re.compile(
    r"(?:code\s+général\s+des\s+impôts|code\s+general\s+des\s+impots|\bCGI\b|même\s+code)",
    re.IGNORECASE,
)

# CGI articles have base numbers from 1 to ~1929. Reject leading zeros, bare 0, etc.
VALID_BASE_RE = re.compile(r"^[1-9]\d*")


def _is_cgi_context(context: str) -> bool:
    """Check if the match context explicitly references the CGI."""
    return bool(CGI_CONTEXT_RE.search(context))


def _is_valid_cgi_article(article_ref: str) -> bool:
    """Check if the article reference looks like a valid CGI article number."""
    m = VALID_BASE_RE.match(article_ref)
    if not m:
        return False
    base = int(m.group())
    # CGI articles go up to ~1960. Annexes can reach ~2500.
    # Beyond that, it's noise (years, other codes, etc.)
    return base <= 2500


def extract_from_debates(engine, extractor):
    """Extract CGI modifications from pre-2002 debate interventions."""
    modifications_by_article = defaultdict(list)
    seen_keys = defaultdict(set)  # Dedup: article -> set of (law_numero, nature)
    skipped_no_context = 0

    with engine.connect() as conn:
        rows = conn.execute(text(QUERY), {"cutoff": CUTOFF_DATE}).fetchall()
        logger.info(f"Processing {len(rows)} interventions mentioning CGI...")

        for contenu, law_numero, law_type, law_year, law_date_promulgation, _ in rows:
            if not contenu:
                continue

            refs = extractor.extract_cgi_references(contenu)
            date_str = law_date_promulgation.strftime("%Y-%m-%d") if law_date_promulgation else ""

            for ref in refs:
                # Only keep refs where context explicitly mentions CGI
                if not _is_cgi_context(ref.context):
                    skipped_no_context += 1
                    continue

                # Reject invalid article numbers (leading zeros, etc.)
                if not _is_valid_cgi_article(ref.article_ref):
                    continue

                # Dedup: one modification per (cgi_article, law, nature)
                dedup_key = (law_numero, ref.modification_type)
                if dedup_key in seen_keys[ref.article_ref]:
                    continue
                seen_keys[ref.article_ref].add(dedup_key)

                nature = _map_nature(ref.modification_type)

                mod = ExtractedModification(
                    cgi_article=ref.article_ref,
                    law_numero=law_numero,
                    law_type=law_type or "",
                    law_year=law_year or 0,
                    article_in_law="(débat)",  # Not from a specific article
                    date_promulgation=date_str,
                    nature=nature,
                    context=ref.context[:200] if ref.context else "",
                    confidence=0.7,  # Debate-sourced, lower than direct article extraction
                )
                modifications_by_article[ref.article_ref].append(mod)

        logger.info(f"Skipped {skipped_no_context} refs without explicit CGI context")

    total = sum(len(v) for v in modifications_by_article.values())
    logger.info(f"Extracted {total} unique modifications for {len(modifications_by_article)} CGI articles")
    return dict(modifications_by_article)


def _map_nature(modification_type: str) -> str:
    """Map CGIExtractor modification_type to index nature."""
    mapping = {
        "modifie": "Modification",
        "abroge": "Abrogation",
        "cree": "Création",
        "complete": "Modification",
        "rédige": "Modification",
        "insere": "Création",
        "mentionne": "Mention",
    }
    return mapping.get(modification_type, "Mention")


def main():
    parser = argparse.ArgumentParser(description="Extract CGI mods from pre-2002 debates")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-confidence", type=float, default=0.5)
    args = parser.parse_args()

    # Load existing index
    if not INDEX_PATH.exists():
        logger.error(f"Index not found: {INDEX_PATH}")
        return 1
    with open(INDEX_PATH, encoding="utf-8") as f:
        existing_index = json.load(f)
    existing_articles = existing_index.get("articles", {})
    existing_mod_count = sum(len(a.get("modifications", [])) for a in existing_articles.values())
    logger.info(f"Existing index: {len(existing_articles)} articles, {existing_mod_count} modifications")

    # Extract from debates
    engine = get_engine()
    extractor = CGIExtractor()
    debate_mods = extract_from_debates(engine, extractor)

    if not debate_mods:
        logger.info("No modifications found.")
        return 0

    # Filter to pre-2002 only (safety check)
    for article_ref in list(debate_mods.keys()):
        debate_mods[article_ref] = [
            m for m in debate_mods[article_ref]
            if m.date_promulgation < CUTOFF_DATE
        ]
        if not debate_mods[article_ref]:
            del debate_mods[article_ref]

    # Merge
    logger.info("Merging into existing index...")
    merged_index = build_article_index(debate_mods, existing_index)

    # Enrich
    session = get_session()
    try:
        merged_index = enrich_from_reference(merged_index, REFERENCE_PATH)
        law_titles = load_law_titles(session)
        merged_index = enrich_law_titles(merged_index, law_titles)
        merged_index = generate_article_keywords(merged_index)
    finally:
        session.close()

    # Stats
    merged_articles = merged_index.get("articles", {})
    new_mod_count = sum(len(a.get("modifications", [])) for a in merged_articles.values())
    added_mods = new_mod_count - existing_mod_count
    new_articles = len(merged_articles) - len(existing_articles)

    pre2002 = sum(1 for a in merged_articles.values() for m in a["modifications"] if m["date"] < CUTOFF_DATE)
    post2002_new = sum(1 for a in merged_articles.values() for m in a["modifications"] if m["date"] >= CUTOFF_DATE)
    post2002_old = sum(1 for a in existing_articles.values() for m in a["modifications"] if m["date"] >= CUTOFF_DATE)

    print(f"\n{'DRY RUN - ' if args.dry_run else ''}Results:")
    print(f"  Articles: {len(existing_articles)} → {len(merged_articles)} (+{new_articles})")
    print(f"  Modifications: {existing_mod_count} → {new_mod_count} (+{added_mods})")
    print(f"  Pre-2002 modifications: {pre2002}")
    print(f"  Post-2002 modifications: {post2002_new} (was {post2002_old})")

    if post2002_new != post2002_old:
        logger.error(f"POST-2002 COUNT CHANGED! Aborting.")
        return 1

    # Sample
    if added_mods > 0:
        print(f"\n  Sample new modifications:")
        count = 0
        for ref, data in sorted(merged_articles.items()):
            existing_keys = set()
            if ref in existing_articles:
                existing_keys = {
                    (m["law_numero"], m["article_in_law"])
                    for m in existing_articles[ref].get("modifications", [])
                }
            for m in data.get("modifications", []):
                if (m["law_numero"], m["article_in_law"]) not in existing_keys:
                    print(f"    {ref} ← {m['law_numero']} ({m['date']}) [{m['nature']}]")
                    count += 1
                    if count >= 20:
                        break
            if count >= 20:
                break

    if args.dry_run:
        print("\nDry run complete. No files modified.")
        return 0

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(merged_index, f, ensure_ascii=False, indent=2)
    logger.info(f"Index saved to {INDEX_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
