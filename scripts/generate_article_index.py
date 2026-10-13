#!/usr/bin/env python3
"""
Generate article_modifications.json by scanning the database for CGI article references.

This script extracts CGI article modifications from the law article texts in the database
using regex patterns, then generates a comprehensive article_modifications.json file.

Usage:
    python scripts/generate_article_index.py
    python scripts/generate_article_index.py --output data/article_modifications_auto.json
    python scripts/generate_article_index.py --min-confidence 0.7
    python scripts/generate_article_index.py --merge  # Merge with existing index
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.analysis.cgi_extractor import CGIExtractor, extract_cgi_modifications_from_db
from src.analysis.keyword_generator import generate_all_keywords
from src.db.models import get_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def generate_keywords(article_ref: str, titre: str = "") -> list[str]:
    """Generate search keywords for an article reference using the keyword generator."""
    return generate_all_keywords(article_ref, titre=titre, include_curated=True)


def build_article_index(
    modifications_by_article: dict,
    existing_index: dict | None = None,
) -> dict:
    """
    Build the article_modifications.json structure from extracted modifications.

    When an existing_index is provided, curated data is preserved: jurisprudence,
    curated_quotes, synthese, synthese_evolution, forward_looking, rapports_parlementaires,
    dila_sources, and any non-empty legifrance_url / bofip_url are kept intact.
    Only the modifications list is merged with new auto-extracted data.

    Args:
        modifications_by_article: Dict mapping CGI article refs to modification lists
        existing_index: Existing index to merge with (optional)

    Returns:
        Complete index structure
    """
    import copy

    articles = {}

    # Curated fields that should never be overwritten by auto-extraction
    CURATED_FIELDS = [
        "jurisprudence",
        "curated_quotes",
        "synthese",
        "synthese_evolution",
        "forward_looking",
        "rapports_parlementaires",
        "dila_sources",
    ]

    # If merging, deep-copy existing articles to preserve all nested data
    if existing_index:
        articles = copy.deepcopy(existing_index.get("articles", {}))

    for article_ref, mods in modifications_by_article.items():
        if not mods:
            continue

        # Check if this article already exists in the index
        if article_ref in articles:
            existing = articles[article_ref]
            # Merge modifications, avoiding duplicates
            existing_mods = {
                (m["law_numero"], m["article_in_law"])
                for m in existing.get("modifications", [])
            }

            for mod in mods:
                mod_key = (mod.law_numero, mod.article_in_law)
                if mod_key not in existing_mods:
                    existing["modifications"].append({
                        "date": mod.date_promulgation,
                        "law_numero": mod.law_numero,
                        "law_type": mod.law_type,
                        "law_year": mod.law_year,
                        "law_title": "",  # Will need to be filled in
                        "article_in_law": mod.article_in_law,
                        "nature": mod.nature,
                        "legifrance_url": "",
                    })
                    existing_mods.add(mod_key)

            # Sort modifications by date
            existing["modifications"].sort(key=lambda m: m["date"])
            # All curated fields are already preserved via deep-copy
            continue

        # Create new article entry
        article_data = {
            "titre": "",  # Will be enriched from reference if available
            "code": "CGI",
            "legifrance_url": "",
            "bofip_url": "",
            "keywords": [],  # Will be generated after enrichment
            "modifications": [],
        }

        # Add modifications
        for mod in mods:
            article_data["modifications"].append({
                "date": mod.date_promulgation,
                "law_numero": mod.law_numero,
                "law_type": mod.law_type,
                "law_year": mod.law_year,
                "law_title": "",
                "article_in_law": mod.article_in_law,
                "nature": mod.nature,
                "legifrance_url": "",
            })

        # Sort modifications by date
        article_data["modifications"].sort(key=lambda m: m["date"])

        articles[article_ref] = article_data

    return {"articles": articles}


def enrich_from_reference(index: dict, reference_path: Path) -> dict:
    """
    Enrich the index with titles and themes from cgi_articles_reference.json.

    Args:
        index: Generated index
        reference_path: Path to cgi_articles_reference.json

    Returns:
        Enriched index
    """
    if not reference_path.exists():
        logger.warning(f"Reference file not found: {reference_path}")
        return index

    with open(reference_path, encoding="utf-8") as f:
        reference = json.load(f)

    ref_by_article = {
        art["reference"]: art for art in reference.get("articles", [])
    }

    for article_ref, article_data in index.get("articles", {}).items():
        if article_ref in ref_by_article:
            ref = ref_by_article[article_ref]
            if not article_data.get("titre"):
                article_data["titre"] = ref.get("titre", "")
            # Add theme as first keyword if not present
            theme = ref.get("theme", "")
            if theme and theme not in article_data.get("keywords", []):
                article_data["keywords"].insert(0, theme)

    return index


def load_law_titles(db_session) -> dict:
    """Load law titles from database for enrichment."""
    from src.db.models import Loi

    titles = {}
    for loi in db_session.query(Loi).all():
        titles[loi.numero] = loi.titre
    return titles


def enrich_law_titles(index: dict, law_titles: dict) -> dict:
    """Add law titles to modifications in the index."""
    for article_data in index.get("articles", {}).values():
        for mod in article_data.get("modifications", []):
            if not mod.get("law_title") and mod.get("law_numero") in law_titles:
                mod["law_title"] = law_titles[mod["law_numero"]]
    return index


def generate_article_keywords(index: dict) -> dict:
    """Generate keywords for all articles using the keyword generator."""
    for article_ref, article_data in index.get("articles", {}).items():
        titre = article_data.get("titre", "")
        existing_keywords = set(article_data.get("keywords", []))
        generated_keywords = set(generate_keywords(article_ref, titre))
        # Merge existing and generated, preserving existing order
        merged = list(existing_keywords) + [k for k in generated_keywords if k not in existing_keywords]
        article_data["keywords"] = merged
    return index


def print_summary(index: dict):
    """Print a summary of the generated index."""
    articles = index.get("articles", {})
    total_mods = sum(len(a.get("modifications", [])) for a in articles.values())
    articles_with_titles = sum(1 for a in articles.values() if a.get("titre"))

    print(f"\nGenerated Index Summary")
    print(f"=" * 50)
    print(f"Total CGI articles: {len(articles)}")
    print(f"Articles with titles: {articles_with_titles} ({100*articles_with_titles/len(articles):.1f}%)" if articles else "")
    print(f"Total modifications: {total_mods}")
    print()

    # Group by number of modifications
    by_mod_count = {}
    for ref, data in articles.items():
        count = len(data.get("modifications", []))
        if count not in by_mod_count:
            by_mod_count[count] = []
        by_mod_count[count].append(ref)

    print("Articles by modification count:")
    for count in sorted(by_mod_count.keys(), reverse=True):
        refs = by_mod_count[count]
        if len(refs) <= 5:
            print(f"  {count} modifications: {', '.join(refs)}")
        else:
            print(f"  {count} modifications: {len(refs)} articles")

    print()

    # Show articles with most modifications
    sorted_articles = sorted(
        articles.items(),
        key=lambda x: len(x[1].get("modifications", [])),
        reverse=True,
    )
    print("Top 10 most-modified articles:")
    for ref, data in sorted_articles[:10]:
        count = len(data.get("modifications", []))
        titre = data.get("titre", "(no title)")[:50]
        print(f"  {ref}: {count} modifications - {titre}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate article_modifications.json from database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=project_root / "data" / "article_modifications_auto.json",
        help="Output file path",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.5,
        help="Minimum confidence score for extracted references (0.0-1.0)",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Merge with existing article_modifications.json",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Update article_modifications.json in place (implies --merge)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Determine output path
    if args.in_place:
        args.merge = True
        args.output = project_root / "data" / "article_modifications.json"

    # Load existing index if merging
    existing_index = None
    if args.merge:
        existing_path = project_root / "data" / "article_modifications.json"
        if existing_path.exists():
            with open(existing_path, encoding="utf-8") as f:
                existing_index = json.load(f)
            logger.info(f"Loaded existing index with {len(existing_index.get('articles', {}))} articles")

    # Connect to database
    print("Connecting to database...")
    session = get_session()

    try:
        # Extract modifications from database
        print("Extracting CGI references from law articles...")
        modifications = extract_cgi_modifications_from_db(
            session,
            min_confidence=args.min_confidence,
        )

        # Build index
        print("Building article index...")
        index = build_article_index(modifications, existing_index)

        # Enrich with reference data
        reference_path = project_root / "data" / "cgi_articles_reference.json"
        index = enrich_from_reference(index, reference_path)

        # Enrich with law titles
        law_titles = load_law_titles(session)
        index = enrich_law_titles(index, law_titles)

        # Generate keywords (after enrichment so titles are available)
        print("Generating keywords...")
        index = generate_article_keywords(index)

        # Write output
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

        print(f"\nIndex saved to: {args.output}")

        # Print summary
        print_summary(index)

    finally:
        session.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
