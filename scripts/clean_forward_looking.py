#!/usr/bin/env python3
"""Purge garbage forward_looking entries from article_modifications.json.

Garbage entries have only LLM-generated propositions with no real citations.
Real entries have at least one citation with actual content (orateur + citation text).

Usage:
    python scripts/clean_forward_looking.py          # Dry run
    python scripts/clean_forward_looking.py --apply   # Apply changes
"""
import argparse
import json
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
INDEX_PATH = PROJECT_ROOT / "data" / "article_modifications.json"


def has_real_citations(entry: dict) -> bool:
    """Check if a forward_looking entry has at least one real citation.

    A real citation has both a non-empty 'citation' text and an 'orateur'.
    Entries with only 'propositions' and no valid citations are garbage.
    """
    citations = entry.get("citations", [])
    if not citations:
        return False

    for cit in citations:
        citation_text = (cit.get("citation") or "").strip()
        orateur = (cit.get("orateur") or "").strip()
        # A real citation should have substantive text and a speaker
        if citation_text and len(citation_text) > 20 and orateur:
            return True

    return False


def main():
    parser = argparse.ArgumentParser(description="Purge garbage forward_looking entries")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    args = parser.parse_args()

    print(f"Loading {INDEX_PATH}...")
    with open(INDEX_PATH, encoding="utf-8") as f:
        data = json.load(f)

    articles = data["articles"]

    total_entries = 0
    garbage_count = 0
    real_count = 0
    articles_with_fl = 0
    articles_cleaned = 0
    propositions_cleared = 0

    for ref, art in articles.items():
        fl_list = art.get("forward_looking", [])
        if not fl_list:
            continue

        articles_with_fl += 1
        total_entries += len(fl_list)

        cleaned = []
        for entry in fl_list:
            if has_real_citations(entry):
                real_count += 1
                cleaned.append(entry)
            else:
                garbage_count += 1

        if len(cleaned) < len(fl_list):
            articles_cleaned += 1
            if args.apply:
                if cleaned:
                    art["forward_looking"] = cleaned
                else:
                    del art["forward_looking"]

    print(f"\nResults:")
    print(f"  Articles with forward_looking:  {articles_with_fl}")
    print(f"  Total entries:                   {total_entries}")
    print(f"  Garbage (to remove):             {garbage_count}")
    print(f"  Real entries (to keep):          {real_count}")
    print(f"  Articles to clean:               {articles_cleaned}")

    if not args.apply:
        print(f"\nDRY RUN: No changes made. Use --apply to save.")
        return

    # Backup and save
    backup_path = INDEX_PATH.with_suffix(".json.bak")
    shutil.copy2(INDEX_PATH, backup_path)
    print(f"  Backup saved to {backup_path}")

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  Saved cleaned index to {INDEX_PATH}")


if __name__ == "__main__":
    main()
