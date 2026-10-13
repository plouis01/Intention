#!/usr/bin/env python3
"""
CLI script to generate legislative intent analysis for CGI articles.

Usage:
    python scripts/analyse_article.py "150-0 B ter"
    python scripts/analyse_article.py "787 B" --question "Comment fonctionne le Dutreil?"
    python scripts/analyse_article.py --list  # List available articles
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.analysis.article_analyzer import ArticleAnalyzer
from src.analysis.article_index import get_article_index
from src.analysis.renderer import render_article_analysis
from src.indexing.search import SearchService
from src.utils.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def list_articles():
    """List all articles available in the index."""
    index = get_article_index()
    articles = index.list_articles()

    if not articles:
        print("No articles found in index.")
        print("Add articles to data/article_modifications.json")
        return

    print(f"\nAvailable articles ({len(articles)}):\n")
    for ref in articles:
        info = index.get_article(ref)
        if info:
            print(f"  {ref}")
            print(f"    {info.titre}")
            print(f"    {len(info.modifications)} modification(s)")
            print()


def check_prerequisites():
    """Check that prerequisites are met."""
    settings = get_settings()

    # Check LLM provider
    if settings.llm_provider == "groq" and not settings.get_groq_api_keys():
        print("ERROR: GROQ_API_KEY(S) not configured")
        print("Set GROQ_API_KEYS (comma-separated) or GROQ_API_KEY in .env, or use LLM_PROVIDER=anthropic")
        return False
    elif settings.llm_provider == "anthropic" and not settings.anthropic_api_key:
        print("ERROR: ANTHROPIC_API_KEY not configured")
        print("Set ANTHROPIC_API_KEY in .env or use LLM_PROVIDER=groq")
        return False

    return True


def analyze_article(
    article_ref: str,
    question: str | None = None,
    output_path: Path | None = None,
    max_modifications: int | None = None,
):
    """
    Analyze an article and generate markdown document.

    Args:
        article_ref: Article reference (e.g., "150-0 B ter")
        question: Optional question to answer
        output_path: Output file path (auto-generated if None)
        max_modifications: Limit number of modifications to analyze
    """
    # Default question
    if not question:
        question = f"Pourquoi le legislateur a-t-il cree et modifie l'article {article_ref} ?"

    # Check prerequisites
    if not check_prerequisites():
        return False

    # Load search service using configured vector store provider
    print(f"\nLoading vector stores...")
    search_service = SearchService.create_configured()

    # Create analyzer
    analyzer = ArticleAnalyzer(search_service=search_service)

    # Check article exists
    index = get_article_index()
    if not index.get_article(article_ref):
        print(f"\nERROR: Article '{article_ref}' not found in index.")
        print("Available articles:")
        for ref in index.list_articles():
            print(f"  - {ref}")
        print("\nTo add a new article, edit data/article_modifications.json")
        return False

    # Run analysis
    print(f"\nAnalyzing article {article_ref}...")
    print(f"Question: {question}\n")

    try:
        result = analyzer.analyze_article(
            article_ref=article_ref,
            question=question,
            max_modifications=max_modifications,
        )
    except Exception as e:
        print(f"ERROR: Analysis failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Render to markdown
    markdown = render_article_analysis(result)

    # Determine output path
    if output_path is None:
        safe_ref = article_ref.replace(" ", "_").replace("-", "_")
        output_path = project_root / "data" / f"analyse_{safe_ref}.md"

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(markdown)

    print(f"\nAnalysis complete!")
    print(f"Output saved to: {output_path}")
    print(f"\nStatistics:")
    print(f"  Modifications analyzed: {len(result.sections)}")
    print(f"  Debates searched: {result.total_debates_searched}")
    print(f"  Interventions found: {result.total_interventions_found}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Generate legislative intent analysis for CGI articles",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "150-0 B ter"
  %(prog)s "787 B" --question "Quelles sont les conditions du Dutreil?"
  %(prog)s "150-0 B ter" --output rapport.md
  %(prog)s --list
        """,
    )

    parser.add_argument(
        "article",
        nargs="?",
        help="CGI article reference (e.g., '150-0 B ter', '787 B')",
    )
    parser.add_argument(
        "-q", "--question",
        help="Question to answer about the article",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        help="Output file path (default: data/analyse_<article>.md)",
    )
    parser.add_argument(
        "-m", "--max-modifications",
        type=int,
        help="Limit number of modifications to analyze",
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="List available articles in the index",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list:
        list_articles()
        return 0

    if not args.article:
        parser.print_help()
        print("\nERROR: Please provide an article reference or use --list")
        return 1

    success = analyze_article(
        article_ref=args.article,
        question=args.question,
        output_path=args.output,
        max_modifications=args.max_modifications,
    )

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
