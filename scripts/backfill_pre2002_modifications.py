#!/usr/bin/env python3
"""Backfill pre-2002 CGI article modifications from local JORF JSON files.

For each law in data/lois_cgi_1958_2026.json with a corresponding raw JORF
file in data/raw/legifrance/, extracts the legi_articles field and adds any
missing modifications to data/article_modifications.json.

No API calls required — uses only already-downloaded files.

Usage:
    python scripts/backfill_pre2002_modifications.py --dry-run
    python scripts/backfill_pre2002_modifications.py --dry-run --year-max 1979
    python scripts/backfill_pre2002_modifications.py
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

RAW_DIR = PROJECT_ROOT / "data" / "raw" / "legifrance"
LOIS_FILE = PROJECT_ROOT / "data" / "lois_cgi_1958_2026.json"
INDEX_FILE = PROJECT_ROOT / "data" / "article_modifications.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill pre-2002 CGI modifications from JORF files")
    parser.add_argument("--dry-run", action="store_true", help="Show stats without writing")
    parser.add_argument("--year-max", type=int, default=2001, help="Only process laws up to this year (default: 2001)")
    parser.add_argument("--year-min", type=int, default=0, help="Only process laws from this year onwards")
    args = parser.parse_args()

    with open(LOIS_FILE, encoding="utf-8") as f:
        lois_data = json.load(f)
    lois = lois_data["lois"]

    with open(INDEX_FILE, encoding="utf-8") as f:
        index = json.load(f)
    articles = index["articles"]

    known_articles = set(articles.keys())

    # Build deduplication index: (num, loi_numero, date) → True
    existing: set[tuple[str, str, str]] = set()
    for num, art in articles.items():
        for m in art.get("modifications", []):
            numero = m.get("law_numero") or m.get("loi_numero", "")
            existing.add((num, numero, m.get("date", "")))

    pre2002_before = sum(
        1 for art in articles.values()
        for m in art.get("modifications", [])
        if m.get("date", "") < "2002-01-01"
    )
    post2002_before = sum(
        1 for art in articles.values()
        for m in art.get("modifications", [])
        if m.get("date", "") >= "2002-01-01"
    )
    print(f"Before: {pre2002_before} pre-2002 mods, {post2002_before} post-2002 mods")

    # Filter laws to process
    year_min_str = f"{args.year_min:04d}-01-01" if args.year_min else "0000-01-01"
    year_max_str = f"{args.year_max:04d}-12-31"
    target_lois = [
        l for l in lois
        if year_min_str <= l["date_promulgation"] <= year_max_str
        and l.get("jorf")
    ]
    print(f"Laws to process: {len(target_lois)} (up to {args.year_max})")

    added = 0
    skipped_no_file = 0
    skipped_dup = 0
    skipped_unknown = 0
    by_year: Counter = Counter()
    by_nature: Counter = Counter()

    for loi in target_lois:
        jorf_path = RAW_DIR / f"{loi['jorf']}.json"
        if not jorf_path.exists():
            skipped_no_file += 1
            continue

        try:
            raw = json.loads(jorf_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            skipped_no_file += 1
            continue

        legi_arts = raw.get("legi_articles", [])
        if not legi_arts:
            continue

        for la in legi_arts:
            num = (la.get("num") or "").strip()
            if not num or num not in known_articles:
                skipped_unknown += 1
                continue

            # Always use the law's promulgation date — legi_articles.date_debut reflects
            # the current version's effective date, not when this specific law modified it.
            date = loi["date_promulgation"]

            loi_num = loi["numero"]
            key = (num, loi_num, date)
            if key in existing:
                skipped_dup += 1
                continue

            # Determine nature
            etat = (la.get("etat") or "").upper()
            existing_dates = [
                m["date"] for m in articles[num].get("modifications", [])
            ]
            if etat == "ABROGE":
                nature = "Abrogation"
            elif not existing_dates or date < min(existing_dates):
                nature = "Création"
            else:
                nature = "Modification"

            mod = {
                "date": date,
                "law_numero": loi_num,
                "law_title": loi["titre"],
                "law_type": loi["type"],
                "law_year": int(date[:4]) if date else 0,
                "nature": nature,
                "source": "jorf_legi_articles",
            }

            if args.dry_run:
                if added < 20:
                    print(f"  [DRY] Art.{num} ← {loi_num} ({date}) [{nature}]")
            else:
                articles[num].setdefault("modifications", []).append(mod)

            existing.add(key)
            added += 1
            by_year[date[:4]] += 1
            by_nature[nature] += 1

    print(f"\nResults: +{added} new modifications")
    print(f"  Skipped — no file: {skipped_no_file} laws")
    print(f"  Skipped — already recorded: {skipped_dup}")
    print(f"  Skipped — article not in index: {skipped_unknown}")
    print(f"\nBy nature: {dict(by_nature)}")
    print(f"\nBy year (new mods):")
    for y in sorted(by_year):
        print(f"  {y}: {by_year[y]}")

    if args.dry_run:
        print("\nDry run — no files modified.")
        return 0

    # Sort modifications by date for each article
    for art in articles.values():
        art["modifications"].sort(key=lambda m: m.get("date", ""))

    # Verify post-2002 count is unchanged
    post2002_after = sum(
        1 for art in articles.values()
        for m in art.get("modifications", [])
        if m.get("date", "") >= "2002-01-01"
    )
    assert post2002_after == post2002_before, (
        f"Post-2002 count changed: {post2002_before} → {post2002_after}"
    )

    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"\nSaved to {INDEX_FILE}")
    print(f"Post-2002 preserved: {post2002_after}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
