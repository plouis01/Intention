#!/usr/bin/env python3
"""
Batch processing script for analyzing multiple laws or CGI articles.

Supports resumability, progress tracking, and rate limiting.

Usage:
    # Analyze all laws
    python scripts/batch_analyse.py --all-laws

    # Analyze all CGI articles from index
    python scripts/batch_analyse.py --all-cgi-articles

    # Analyze indexed articles (from article_modifications.json)
    python scripts/batch_analyse.py --indexed-articles

    # Limit to N articles (for testing)
    python scripts/batch_analyse.py --indexed-articles --max-articles 10

    # Skip articles that already have an analysis file
    python scripts/batch_analyse.py --indexed-articles --skip-existing

    # With Anthropic fallback for rate limiting
    python scripts/batch_analyse.py --indexed-articles --anthropic-fallback

    # Analyze laws for specific years
    python scripts/batch_analyse.py --years 2020-2025

    # Analyze specific law types
    python scripts/batch_analyse.py --all-laws --types LF,LFR

    # Resume a previous batch
    python scripts/batch_analyse.py --resume

    # Resume a specific batch
    python scripts/batch_analyse.py --resume --batch-id abc12345

    # List all batches
    python scripts/batch_analyse.py --list-batches

    # Skip LLM analysis for faster processing
    python scripts/batch_analyse.py --all-laws --no-intent
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.analysis.batch_processor import (
    BatchProcessor,
    get_all_cgi_articles,
    get_all_laws,
    get_indexed_articles,
)
from src.analysis.law_analyzer import LawAnalyzer
from src.analysis.article_analyzer import ArticleAnalyzer
from src.analysis.renderer import render_article_analysis, render_law_analysis
from src.indexing.search import SearchService
from src.utils.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def print_progress(state):
    """Print progress update."""
    pct = state.progress_pct
    completed = len(state.completed_items)
    failed = len(state.failed_items)
    total = state.total_items

    print(f"\rProgress: {pct:.1f}% ({completed}/{total} completed, {failed} failed)", end="", flush=True)


def process_law(
    law_ref: str,
    analyzer: LawAnalyzer,
    output_dir: Path,
    analyze_intent: bool,
) -> bool:
    """
    Process a single law.

    Args:
        law_ref: Law reference like "LF_2024"
        analyzer: LawAnalyzer instance
        output_dir: Output directory
        analyze_intent: Whether to analyze intent with LLM

    Returns:
        True if successful
    """
    try:
        # Parse reference
        parts = law_ref.split("_")
        if len(parts) != 2:
            logger.error(f"Invalid law reference: {law_ref}")
            return False

        law_type, year_str = parts
        law_year = int(year_str)

        # Analyze
        result = analyzer.analyze_law(
            law_type=law_type,
            law_year=law_year,
            analyze_intent=analyze_intent,
        )

        # Render and save
        markdown = render_law_analysis(result)
        output_path = output_dir / f"{law_type}_{law_year}.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(markdown)

        return True

    except Exception as e:
        logger.error(f"Failed to process {law_ref}: {e}")
        return False


def sanitize_article_ref(article_ref: str) -> str:
    """Convert article ref to safe filename."""
    return article_ref.replace(" ", "_").replace("-", "_")


def process_article(
    article_ref: str,
    analyzer: ArticleAnalyzer,
    output_dir: Path,
    skip_existing: bool = False,
) -> bool:
    """
    Process a single article.

    Args:
        article_ref: Article reference like "150-0 B ter"
        analyzer: ArticleAnalyzer instance
        output_dir: Output directory
        skip_existing: Skip if output file already exists

    Returns:
        True if successful
    """
    try:
        safe_ref = sanitize_article_ref(article_ref)
        output_path = output_dir / f"{safe_ref}.md"

        if skip_existing and output_path.exists():
            logger.info(f"Skipping {article_ref} (already exists)")
            return True

        # Default question
        question = f"Quelle est l'intention du législateur concernant l'article {article_ref} ?"

        # Analyze
        result = analyzer.analyze_article(
            article_ref=article_ref,
            question=question,
        )

        # Render and save
        markdown = render_article_analysis(result)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(markdown)

        return True

    except Exception as e:
        logger.error(f"Failed to process {article_ref}: {e}")
        return False


def run_laws_batch(
    years: tuple[int, int] | None,
    law_types: list[str] | None,
    analyze_intent: bool,
    resume_batch_id: str | None,
    rate_limit: float,
):
    """Run batch processing for laws."""
    processor = BatchProcessor(
        state_dir=project_root / "data" / "batch_state",
        rate_limit_delay=rate_limit,
    )
    output_dir = project_root / "data" / "analyses" / "by_law"

    # Get or create batch
    if resume_batch_id:
        state = processor.load_batch(resume_batch_id)
        if not state:
            print(f"ERROR: Batch {resume_batch_id} not found")
            return False
        print(f"Resuming batch {state.batch_id}")
    else:
        items = get_all_laws(years=years, law_types=law_types)
        if not items:
            print("No laws found matching criteria")
            return False
        state = processor.create_batch("laws", items)
        print(f"Created batch {state.batch_id} with {len(items)} laws")

    # Create analyzer
    analyzer = LawAnalyzer()

    # Process
    print(f"\nProcessing {state.total_items} laws...")
    print(f"Output directory: {output_dir}")
    print()

    def process_func(item: str) -> bool:
        return process_law(item, analyzer, output_dir, analyze_intent)

    state = processor.process_batch(state, process_func, print_progress)
    print()  # Newline after progress

    # Summary
    summary = processor.get_batch_summary(state.batch_id)
    print(f"\nBatch {state.batch_id} complete!")
    print(f"  Completed: {summary['completed']}")
    print(f"  Failed: {summary['failed']}")
    print(f"  Output: {output_dir}")

    if summary['failed'] > 0:
        print(f"\nFailed items: {state.failed_items}")

    return summary['failed'] == 0


def run_articles_batch(
    use_indexed: bool,
    resume_batch_id: str | None,
    rate_limit: float,
    max_articles: int | None = None,
    skip_existing: bool = False,
    anthropic_fallback: bool = False,
):
    """Run batch processing for articles.

    Args:
        use_indexed: If True, use articles from article_modifications.json index.
        resume_batch_id: Batch ID to resume, or None for new batch.
        rate_limit: Seconds between items.
        max_articles: Limit number of articles to process (for testing).
        skip_existing: Skip articles that already have an output file.
        anthropic_fallback: Fall back to Anthropic when Groq keys are exhausted.
    """
    processor = BatchProcessor(
        state_dir=project_root / "data" / "batch_state",
        rate_limit_delay=rate_limit,
    )
    output_dir = project_root / "data" / "analyses" / "by_article"

    # Check prerequisites
    settings = get_settings()
    if settings.llm_provider == "groq" and not settings.get_groq_api_keys():
        print("ERROR: GROQ_API_KEY(S) not configured")
        return False
    elif settings.llm_provider == "anthropic" and not settings.anthropic_api_key:
        print("ERROR: ANTHROPIC_API_KEY not configured")
        return False

    # Configure Groq pool with enrichment-specific settings
    if settings.llm_provider == "groq":
        from src.utils.groq_pool import get_groq_pool, reset_default_pool
        reset_default_pool()
        get_groq_pool(call_delay=rate_limit, anthropic_fallback=anthropic_fallback)

    # Get or create batch
    if resume_batch_id:
        state = processor.load_batch(resume_batch_id)
        if not state:
            print(f"ERROR: Batch {resume_batch_id} not found")
            return False
        print(f"Resuming batch {state.batch_id}")
    else:
        if use_indexed:
            items = get_indexed_articles()
        else:
            items = get_all_cgi_articles()

        if not items:
            print("No articles found")
            return False

        if max_articles:
            items = items[:max_articles]
            print(f"Limited to {max_articles} articles")

        state = processor.create_batch("articles", items)
        print(f"Created batch {state.batch_id} with {len(items)} articles")

    # Load search service using configured provider
    print("Loading vector stores...")
    search_service = SearchService.create_configured()

    # Create analyzer
    analyzer = ArticleAnalyzer(search_service=search_service)

    # Process
    print(f"\nProcessing {state.total_items} articles...")
    print(f"Output directory: {output_dir}")
    if skip_existing:
        print("Skipping articles with existing output files")
    print()

    def process_func(item: str) -> bool:
        return process_article(item, analyzer, output_dir, skip_existing=skip_existing)

    state = processor.process_batch(state, process_func, print_progress)
    print()  # Newline after progress

    # Summary
    summary = processor.get_batch_summary(state.batch_id)
    print(f"\nBatch {state.batch_id} complete!")
    print(f"  Completed: {summary['completed']}")
    print(f"  Failed: {summary['failed']}")
    print(f"  Output: {output_dir}")

    if summary['failed'] > 0:
        print(f"\nFailed items: {state.failed_items}")

    return summary['failed'] == 0


def list_batches():
    """List all batch processing states."""
    processor = BatchProcessor(
        state_dir=project_root / "data" / "batch_state",
    )

    batches = processor.list_batches()

    if not batches:
        print("No batches found.")
        return

    print(f"\nBatch Processing History ({len(batches)} batches)\n")
    print(f"{'ID':<10} {'Mode':<10} {'Progress':<15} {'Status':<12} {'Started':<20}")
    print("-" * 70)

    for batch in batches:
        batch_id = batch.get("batch_id", "")[:8]
        mode = batch.get("mode", "")
        progress = f"{batch.get('completed', 0)}/{batch.get('total', 0)}"
        status = "Complete" if batch.get("is_complete") else "In Progress"
        started = batch.get("started_at", "")[:19]

        print(f"{batch_id:<10} {mode:<10} {progress:<15} {status:<12} {started:<20}")


def main():
    parser = argparse.ArgumentParser(
        description="Batch process laws or CGI articles",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --all-laws                    # Process all laws
  %(prog)s --all-laws --years 2020-2025  # Laws from 2020-2025
  %(prog)s --all-laws --types LF         # Only LF (not LFR/LFSS)
  %(prog)s --all-cgi-articles            # All CGI articles from reference
  %(prog)s --indexed-articles            # Only articles in index
  %(prog)s --resume                      # Resume latest batch
  %(prog)s --resume --batch-id abc123    # Resume specific batch
  %(prog)s --list-batches                # Show batch history
        """,
    )

    # Mode selection
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--all-laws",
        action="store_true",
        help="Process all finance laws",
    )
    mode_group.add_argument(
        "--all-cgi-articles",
        action="store_true",
        help="Process all CGI articles from reference file",
    )
    mode_group.add_argument(
        "--indexed-articles",
        action="store_true",
        help="Process articles from article_modifications.json",
    )
    mode_group.add_argument(
        "--resume",
        action="store_true",
        help="Resume the latest incomplete batch",
    )
    mode_group.add_argument(
        "--list-batches",
        action="store_true",
        help="List all batch processing states",
    )

    # Filters
    parser.add_argument(
        "--years",
        help="Year range filter (e.g., '2020-2025')",
    )
    parser.add_argument(
        "--types",
        help="Law types to include (e.g., 'LF,LFR')",
    )
    parser.add_argument(
        "--batch-id",
        help="Specific batch ID to resume",
    )

    # Options
    parser.add_argument(
        "--no-intent",
        action="store_true",
        help="Skip LLM intent analysis (faster)",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=1.0,
        help="Seconds between items (default: 1.0)",
    )
    parser.add_argument(
        "--max-articles",
        type=int,
        default=None,
        help="Limit number of articles to process (for testing)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip articles that already have an output file",
    )
    parser.add_argument(
        "--anthropic-fallback",
        action="store_true",
        help="Fall back to Anthropic API when all Groq keys are exhausted",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # List batches
    if args.list_batches:
        list_batches()
        return 0

    # Resume mode
    if args.resume:
        processor = BatchProcessor(
            state_dir=project_root / "data" / "batch_state",
        )

        if args.batch_id:
            state = processor.load_batch(args.batch_id)
        else:
            state = processor.get_latest_batch()

        if not state:
            print("No batch to resume. Start a new batch with --all-laws or --all-cgi-articles")
            return 1

        if state.is_complete:
            print(f"Batch {state.batch_id} is already complete.")
            return 0

        if state.mode == "laws":
            success = run_laws_batch(
                years=None,
                law_types=None,
                analyze_intent=not args.no_intent,
                resume_batch_id=state.batch_id,
                rate_limit=args.rate_limit,
            )
        else:
            success = run_articles_batch(
                use_indexed=False,
                resume_batch_id=state.batch_id,
                rate_limit=args.rate_limit,
                skip_existing=args.skip_existing,
                anthropic_fallback=args.anthropic_fallback,
            )

        return 0 if success else 1

    # Parse filters
    years = None
    if args.years:
        parts = args.years.split("-")
        if len(parts) == 2:
            years = (int(parts[0]), int(parts[1]))

    law_types = None
    if args.types:
        law_types = [t.strip().upper() for t in args.types.split(",")]

    # Run appropriate batch
    if args.all_laws:
        success = run_laws_batch(
            years=years,
            law_types=law_types,
            analyze_intent=not args.no_intent,
            resume_batch_id=None,
            rate_limit=args.rate_limit,
        )
        return 0 if success else 1

    if args.all_cgi_articles:
        success = run_articles_batch(
            use_indexed=False,
            resume_batch_id=None,
            rate_limit=args.rate_limit,
            max_articles=args.max_articles,
            skip_existing=args.skip_existing,
            anthropic_fallback=args.anthropic_fallback,
        )
        return 0 if success else 1

    if args.indexed_articles:
        success = run_articles_batch(
            use_indexed=True,
            resume_batch_id=None,
            rate_limit=args.rate_limit,
            max_articles=args.max_articles,
            skip_existing=args.skip_existing,
            anthropic_fallback=args.anthropic_fallback,
        )
        return 0 if success else 1

    parser.print_help()
    print("\nERROR: Select a mode: --all-laws, --all-cgi-articles, --indexed-articles, --resume, or --list-batches")
    return 1


if __name__ == "__main__":
    sys.exit(main())
