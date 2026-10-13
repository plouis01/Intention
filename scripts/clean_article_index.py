#!/usr/bin/env python3
"""Clean data quality issues in article_modifications.json.

3 passes:
  1. Deduplicate legacy mods with identical (date, nature) per article
  2. Remove legacy mods superseded by richer jorf_legi_articles mods (same date)
  3. Strip article CGI references from loi_titre in jorf_legi_articles mods

Usage:
    python scripts/clean_article_index.py --dry-run
    python scripts/clean_article_index.py
"""

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
INDEX_FILE = PROJECT_ROOT / "data" / "article_modifications.json"

# Strip " - art. X..." or " art. X..." suffix from law titles
SUFFIX_RE = re.compile(r"\s+(?:-\s+)?art\.\s+\S.*$", re.IGNORECASE)


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean article_modifications.json quality issues")
    parser.add_argument("--dry-run", action="store_true", help="Show stats without writing")
    args = parser.parse_args()

    with open(INDEX_FILE, encoding="utf-8") as f:
        data = json.load(f)
    articles = data["articles"]

    total_before = sum(len(a.get("modifications", [])) for a in articles.values())
    post2002_before = sum(
        1 for a in articles.values()
        for m in a.get("modifications", [])
        if m.get("date", "") >= "2002-01-01"
    )
    print(f"Before: {total_before} mods, {post2002_before} post-2002")

    # --- Pass 1: deduplicate intra-legacy (same date + nature) — pre-2002 only ---
    # Post-2002 mods from original pipeline are untouched (different dedup concern)
    pass1_removed = 0
    for art in articles.values():
        seen: set[tuple[str, str]] = set()
        keep = []
        for m in art.get("modifications", []):
            if m.get("source", "legacy") != "legacy" or m.get("date", "") >= "2002-01-01":
                keep.append(m)
                continue
            key = (m.get("date", ""), m.get("nature", ""))
            if key not in seen:
                seen.add(key)
                keep.append(m)
            else:
                pass1_removed += 1
        art["modifications"] = keep
    print(f"Pass 1 (dedup intra-legacy pre-2002): -{pass1_removed} duplicates")

    # --- Pass 2: remove legacy mods superseded by jorf_legi_articles (same date, pre-2002 only) ---
    # jorf_legi_articles only covers pre-2002 laws, so only pre-2002 legacy mods are candidates
    pass2_removed = 0
    for art in articles.values():
        jorf_dates = {
            m["date"]
            for m in art.get("modifications", [])
            if m.get("source") == "jorf_legi_articles"
        }
        new_mods = []
        for m in art.get("modifications", []):
            is_legacy_pre2002 = (
                m.get("source", "legacy") == "legacy"
                and m.get("date", "") < "2002-01-01"
            )
            if is_legacy_pre2002 and m.get("date", "") in jorf_dates:
                pass2_removed += 1
            else:
                new_mods.append(m)
        art["modifications"] = new_mods
    print(f"Pass 2 (legacy superseded by jorf, pre-2002): -{pass2_removed} redundant legacy mods")

    # --- Pass 3: clean loi_titre (strip article CGI references) ---
    pass3_cleaned = 0
    for art in articles.values():
        for m in art.get("modifications", []):
            if m.get("source") == "jorf_legi_articles" and m.get("loi_titre"):
                cleaned = SUFFIX_RE.sub("", m["loi_titre"]).strip()
                if cleaned != m["loi_titre"]:
                    if args.dry_run and pass3_cleaned < 5:
                        print(f"  [TITRE] {repr(m['loi_titre'][:80])} → {repr(cleaned[:80])}")
                    if not args.dry_run:
                        m["loi_titre"] = cleaned
                    pass3_cleaned += 1
    print(f"Pass 3 (clean loi_titre): {pass3_cleaned} titles cleaned")

    total_after = sum(len(a.get("modifications", [])) for a in articles.values())
    print(f"\nAfter: {total_after} mods (removed {total_before - total_after})")

    if args.dry_run:
        print("Dry run — no files modified.")
        return 0

    # Verify post-2002 count unchanged
    post2002_after = sum(
        1 for a in articles.values()
        for m in a.get("modifications", [])
        if m.get("date", "") >= "2002-01-01"
    )
    assert post2002_after == post2002_before, (
        f"Post-2002 count changed: {post2002_before} → {post2002_after}"
    )

    # Re-sort modifications by date
    for art in articles.values():
        art["modifications"].sort(key=lambda m: m.get("date", ""))

    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Saved to {INDEX_FILE}")
    print(f"Post-2002 preserved: {post2002_after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
