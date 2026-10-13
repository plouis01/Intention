#!/usr/bin/env python3
"""
Generate comprehensive article analysis document.

This script runs the full article analysis pipeline to generate
a markdown document similar to data/test_analyse_150_0_B_ter.md.

Requires LLM API key (GROQ_API_KEY or ANTHROPIC_API_KEY) in .env

Usage:
    python scripts/analyze_article.py
    python scripts/analyze_article.py --article "150-0 B ter"
    python scripts/analyze_article.py --article "150-0 B ter" --max-mods 3
    python scripts/analyze_article.py --list
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
from src.utils.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def check_config() -> bool:
    """Check required configuration."""
    settings = get_settings()

    print(f"LLM provider: {settings.llm_provider}")
    print(f"Embedding provider: {settings.embedding_provider}")

    # Check LLM provider
    if settings.llm_provider == "groq":
        if not settings.groq_api_key:
            print("ERROR: GROQ_API_KEY not configured")
            print("Get a free API key at https://console.groq.com/")
            return False
        print(f"Groq model: {settings.groq_model}")
    else:
        if not settings.anthropic_api_key:
            print("ERROR: ANTHROPIC_API_KEY not configured")
            return False
        print(f"Claude model: {settings.claude_model}")

    # Vector stores loaded automatically via SearchService.create_configured()

    return True


def list_available_articles():
    """List all articles available in the index."""
    print("\nAvailable articles in index:")
    print("-" * 40)

    index = get_article_index()
    articles = index.list_articles()

    if not articles:
        print("No articles found in index.")
        print(f"Index file: {index.index_path}")
        return

    for ref in articles:
        article = index.get_article(ref)
        if article:
            print(f"  {ref}: {article.titre}")
            print(f"    Code: {article.code}")
            print(f"    Modifications: {len(article.modifications)}")
            print()


def analyze_article(
    article_ref: str,
    question: str | None = None,
    max_modifications: int | None = None,
    output_path: Path | None = None,
) -> bool:
    """
    Run full article analysis pipeline.

    Args:
        article_ref: Article reference (e.g., "150-0 B ter")
        question: Question to answer (default: "Pourquoi le législateur...")
        max_modifications: Limit modifications to analyze
        output_path: Output file path (default: data/analyse_{ref}.md)

    Returns:
        True if successful
    """
    if question is None:
        question = f"Pourquoi le législateur a-t-il créé et modifié l'article {article_ref} ?"

    print(f"\n{'=' * 60}")
    print(f"Analyzing article: {article_ref}")
    print(f"Question: {question}")
    print(f"{'=' * 60}\n")

    try:
        # Initialize analyzer
        analyzer = ArticleAnalyzer()

        # Check if article exists in index
        article_info = analyzer.article_index.get_article(article_ref)
        if not article_info:
            print(f"ERROR: Article '{article_ref}' not found in index")
            print("Available articles:")
            for ref in analyzer.article_index.list_articles():
                print(f"  - {ref}")
            return False

        print(f"Found article: {article_info.titre}")
        print(f"Code: {article_info.code}")
        print(f"Modifications: {len(article_info.modifications)}")
        if max_modifications:
            print(f"Limiting to: {max_modifications} modifications")
        print()

        # Run analysis
        print("Running analysis pipeline...")
        result = analyzer.analyze_article(
            article_ref=article_ref,
            question=question,
            include_jurisprudence=True,
            max_modifications=max_modifications,
        )

        print(f"\nAnalysis complete:")
        print(f"  Sections analyzed: {len(result.sections)}")
        print(f"  Total debates searched: {result.total_debates_searched}")
        print(f"  Total interventions found: {result.total_interventions_found}")
        print(f"  Jurisprudence entries: {len(result.jurisprudence)}")

        # Render to markdown
        print("\nRendering markdown...")
        markdown = render_article_analysis(result)

        # Determine output path
        if output_path is None:
            safe_ref = article_ref.replace(" ", "_").replace("-", "_")
            output_path = project_root / "data" / f"analyse_{safe_ref}.md"

        # Save result
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(markdown)

        print(f"\nOutput saved to: {output_path}")
        print(f"Document size: {len(markdown):,} characters")

        # Print preview
        print("\n" + "=" * 60)
        print("Preview (first 1000 chars):")
        print("=" * 60)
        print(markdown[:1000])
        print("...")

        return True

    except Exception as e:
        logger.exception(f"Analysis failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Generate comprehensive article analysis document"
    )
    parser.add_argument(
        "--article",
        "-a",
        default="150-0 B ter",
        help="Article reference to analyze (default: 150-0 B ter)",
    )
    parser.add_argument(
        "--question",
        "-q",
        help="Question to answer (default: auto-generated)",
    )
    parser.add_argument(
        "--max-mods",
        "-m",
        type=int,
        help="Maximum number of modifications to analyze",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output file path (default: data/analyse_{article}.md)",
    )
    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="List available articles in index",
    )

    args = parser.parse_args()

    if args.list:
        list_available_articles()
        return 0

    # Check configuration
    if not check_config():
        return 1

    # Run analysis
    success = analyze_article(
        article_ref=args.article,
        question=args.question,
        max_modifications=args.max_mods,
        output_path=args.output,
    )

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
