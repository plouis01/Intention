#!/usr/bin/env python3
"""
Export article-refs.json for the frontend from article_modifications.json.

Usage:
    python scripts/export_article_refs.py
    python scripts/export_article_refs.py --output /path/to/article-refs.json
"""
import argparse
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SOURCE = PROJECT_ROOT / "data" / "article_modifications.json"
DEFAULT_OUTPUT = PROJECT_ROOT.parent / "legal-interface" / "src" / "data" / "article-refs.json"


def main():
    parser = argparse.ArgumentParser(description="Export article-refs.json for the frontend")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    with open(SOURCE, encoding="utf-8") as f:
        data = json.load(f)

    refs = []
    for ref, article in data["articles"].items():
        refs.append({
            "ref": ref,
            "titre": article.get("titre", ""),
            "keywords": article.get("keywords", []),
            "modificationCount": len(article.get("modifications", [])),
        })

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(refs, f, ensure_ascii=False, indent=2)

    print(f"Exported {len(refs)} articles to {args.output}")


if __name__ == "__main__":
    main()
