#!/usr/bin/env python3
"""Purge skeleton jurisprudence entries from article_modifications.json.

Skeleton entries have the old schema (numero/resume) and lack the required
fields (titre, solution, faits). This script removes them while preserving
real curated jurisprudence entries.

Usage:
    python scripts/clean_jurisprudence_skeletons.py          # Dry run
    python scripts/clean_jurisprudence_skeletons.py --apply   # Apply changes
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
INDEX_PATH = PROJECT_ROOT / "data" / "article_modifications.json"


def is_skeleton(entry: dict) -> bool:
    """Check if a jurisprudence entry is an empty skeleton.

    Skeletons are entries generated with the wrong schema (numero/resume)
    that lack the expected fields (titre, solution, faits).
    """
    titre = (entry.get("titre") or "").strip()
    solution = (entry.get("solution") or "").strip()
    faits = (entry.get("faits") or "").strip()

    # If all three substantive fields are empty, it's a skeleton
    return not titre and not solution and not faits


def main():
    parser = argparse.ArgumentParser(description="Purge skeleton jurisprudence entries")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    args = parser.parse_args()

    print(f"Loading {INDEX_PATH}...")
    with open(INDEX_PATH, encoding="utf-8") as f:
        data = json.load(f)

    articles = data["articles"]

    total_entries = 0
    skeleton_count = 0
    real_count = 0
    articles_affected = 0

    for ref, art in articles.items():
        juris = art.get("jurisprudence", [])
        if not juris:
            continue

        total_entries += len(juris)
        real = [e for e in juris if not is_skeleton(e)]
        skeletons = len(juris) - len(real)

        real_count += len(real)
        skeleton_count += skeletons

        if skeletons > 0:
            articles_affected += 1
            if args.apply:
                if real:
                    art["jurisprudence"] = real
                else:
                    del art["jurisprudence"]

    print(f"\nResults:")
    print(f"  Total jurisprudence entries: {total_entries}")
    print(f"  Skeletons (to remove):       {skeleton_count}")
    print(f"  Real entries (to keep):       {real_count}")
    print(f"  Articles affected:            {articles_affected}")

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
