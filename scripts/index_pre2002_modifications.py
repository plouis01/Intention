#!/usr/bin/env python3
"""Add pre-2002 CGI article modifications to article_modifications.json.

Extracts CGI article references from pre-2002 laws in the database and merges
them into the existing index. Post-2002 data is never modified.

Usage:
    python scripts/index_pre2002_modifications.py --dry-run
    python scripts/index_pre2002_modifications.py
    python scripts/index_pre2002_modifications.py --min-confidence 0.7
"""

import argparse
import json
import logging
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.analysis.cgi_extractor import extract_cgi_modifications_from_db
from src.db.models import get_session

# Reuse infrastructure from generate_article_index
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


def filter_pre2002(modifications_by_article: dict) -> dict:
    """Keep only modifications with date_promulgation before 2002."""
    filtered = {}
    kept = 0
    dropped = 0
    for article_ref, mods in modifications_by_article.items():
        pre = [m for m in mods if m.date_promulgation < CUTOFF_DATE]
        if pre:
            filtered[article_ref] = pre
            kept += len(pre)
        dropped += len(mods) - len(pre)
    logger.info(f"Filtered: {kept} pre-2002 modifications kept, {dropped} post-2002 dropped")
    return filtered


def main():
    parser = argparse.ArgumentParser(description="Add pre-2002 CGI modifications to index")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be added without writing")
    parser.add_argument("--min-confidence", type=float, default=0.5, help="Minimum confidence (0.0-1.0)")
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

    # Extract from DB
    session = get_session()
    try:
        logger.info("Extracting CGI references from all laws...")
        all_mods = extract_cgi_modifications_from_db(session, min_confidence=args.min_confidence)

        # Filter to pre-2002 only
        pre2002_mods = filter_pre2002(all_mods)

        if not pre2002_mods:
            logger.info("No pre-2002 modifications found.")
            return 0

        # Merge into existing index
        logger.info("Merging pre-2002 modifications into existing index...")
        merged_index = build_article_index(pre2002_mods, existing_index)

        # Enrich
        merged_index = enrich_from_reference(merged_index, REFERENCE_PATH)
        law_titles = load_law_titles(session)
        merged_index = enrich_law_titles(merged_index, law_titles)
        merged_index = generate_article_keywords(merged_index)

        # Count what changed
        merged_articles = merged_index.get("articles", {})
        new_mod_count = sum(len(a.get("modifications", [])) for a in merged_articles.values())
        added_mods = new_mod_count - existing_mod_count
        new_articles = len(merged_articles) - len(existing_articles)

        # Summary
        print(f"\n{'DRY RUN - ' if args.dry_run else ''}Results:")
        print(f"  Articles: {len(existing_articles)} → {len(merged_articles)} (+{new_articles})")
        print(f"  Modifications: {existing_mod_count} → {new_mod_count} (+{added_mods})")

        # Show details of what's added
        pre2002_total = sum(
            1 for a in merged_articles.values()
            for m in a.get("modifications", [])
            if m["date"] < CUTOFF_DATE
        )
        print(f"  Pre-2002 modifications in final index: {pre2002_total}")

        # Verify post-2002 unchanged
        post2002_new = sum(
            1 for a in merged_articles.values()
            for m in a.get("modifications", [])
            if m["date"] >= CUTOFF_DATE
        )
        post2002_old = sum(
            1 for a in existing_articles.values()
            for m in a.get("modifications", [])
            if m["date"] >= CUTOFF_DATE
        )
        if post2002_new != post2002_old:
            logger.error(f"POST-2002 COUNT CHANGED: {post2002_old} → {post2002_new}. Aborting.")
            return 1
        print(f"  Post-2002 modifications unchanged: {post2002_new}")

        # Show some examples
        if new_articles > 0 or added_mods > 0:
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
                        print(f"    {ref} ← {m['law_numero']} art.{m['article_in_law']} ({m['date']}) [{m['nature']}]")
                        count += 1
                        if count >= 15:
                            break
                if count >= 15:
                    break

        if args.dry_run:
            print("\nDry run complete. No files modified.")
            return 0

        # Write
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump(merged_index, f, ensure_ascii=False, indent=2)
        logger.info(f"Index saved to {INDEX_PATH}")

    finally:
        session.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
