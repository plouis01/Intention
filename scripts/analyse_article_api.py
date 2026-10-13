#!/usr/bin/env python3
"""
API-oriented article analysis CLI.

Same logic as analyse_article.py but persists results to the DB cache
instead of writing a markdown file. Checks cache before running.

Usage:
    python scripts/analyse_article_api.py "150-0 B ter"
    python scripts/analyse_article_api.py "787 B" --question "Comment fonctionne le Dutreil?"
"""

import argparse
import logging
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.analysis.article_analysis_service import analyze_article_for_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def main():
    parser = argparse.ArgumentParser(
        description="Run article analysis and store result in DB cache",
    )
    parser.add_argument("article", nargs="?", help="CGI article reference (e.g., '150-0 B ter')")
    parser.add_argument("-q", "--question", help="Question to answer about the article")
    parser.add_argument("-m", "--max-modifications", type=int)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.article:
        parser.print_help()
        return 1

    result = analyze_article_for_api(
        article_ref=args.article,
        question=args.question,
        max_modifications=args.max_modifications,
    )

    meta = result.get("metadata", {})
    print(f"\nAnalysis complete!")
    print(f"  From cache: {result.get('from_cache', False)}")
    print(f"  Sections: {meta.get('total_sections')}")
    print(f"  Debates searched: {meta.get('total_debates_searched')}")
    print(f"  Generated: {meta.get('generated_date')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
