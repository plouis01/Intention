#!/usr/bin/env python3
"""Clean up data quality issues in article_modifications.json.

Fixes:
1. Strip time suffix from dates ("2019-12-28 00:00:00" -> "2019-12-28")
2. Remove articles with 0 modifications
3. Remove pre-1950 modifications (API artifacts from before CGI codification)

Usage:
    python scripts/cleanup_article_index.py --dry-run
    python scripts/cleanup_article_index.py
"""

import argparse
import json
from pathlib import Path

INDEX_PATH = Path(__file__).parent.parent / "data" / "article_modifications.json"


def main():
    parser = argparse.ArgumentParser(description="Clean up article_modifications.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(INDEX_PATH, encoding="utf-8") as f:
        data = json.load(f)

    articles = data["articles"]
    total_before = len(articles)
    mods_before = sum(len(a["modifications"]) for a in articles.values())

    # 1. Strip time suffix from dates
    dates_fixed = 0
    for art in articles.values():
        for m in art["modifications"]:
            if " " in m.get("date", ""):
                m["date"] = m["date"].split(" ")[0]
                dates_fixed += 1

    # 2. Remove pre-1950 modifications
    pre1950_removed = 0
    for art in articles.values():
        before = len(art["modifications"])
        art["modifications"] = [
            m for m in art["modifications"]
            if m.get("date", "9999") >= "1950-01-01"
        ]
        pre1950_removed += before - len(art["modifications"])

    # 3. Remove articles with 0 modifications
    empty_removed = []
    for ref in list(articles.keys()):
        if not articles[ref]["modifications"]:
            empty_removed.append(ref)
            del articles[ref]

    total_after = len(articles)
    mods_after = sum(len(a["modifications"]) for a in articles.values())

    print(f"{'DRY RUN - ' if args.dry_run else ''}Cleanup results:")
    print(f"  Dates fixed: {dates_fixed}")
    print(f"  Pre-1950 mods removed: {pre1950_removed}")
    print(f"  Empty articles removed: {len(empty_removed)} ({', '.join(empty_removed)})")
    print(f"  Articles: {total_before} -> {total_after}")
    print(f"  Modifications: {mods_before} -> {mods_after}")

    if args.dry_run:
        print("\nDry run complete. No files modified.")
        return

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\nSaved to {INDEX_PATH}")


if __name__ == "__main__":
    main()
