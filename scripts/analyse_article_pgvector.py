#!/usr/bin/env python3
"""
Script d'analyse d'article CGI optimisé pour pgvector.

Se connecte directement à la base PostgreSQL avec pgvector pour
produire la meilleure analyse possible d'un article du CGI.

Usage:
    python scripts/analyse_article_pgvector.py "150-0 B ter"
    python scripts/analyse_article_pgvector.py "787 B" --question "Quelles sont les conditions du Dutreil?"
    python scripts/analyse_article_pgvector.py "150 U" --output rapport.md
    python scripts/analyse_article_pgvector.py --list
"""

import argparse
import logging
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database URL — override via CLI flag or env var
# ---------------------------------------------------------------------------
DEFAULT_DATABASE_URL = os.getenv("DATABASE_URL")


def _apply_db_url(db_url: str) -> None:
    """Force DATABASE_URL and VECTOR_STORE_PROVIDER before any settings load."""
    os.environ["DATABASE_URL"] = db_url
    os.environ["VECTOR_STORE_PROVIDER"] = "pgvector"


def list_articles():
    """List all articles available in the index."""
    from src.analysis.article_index import get_article_index

    index = get_article_index()
    articles = index.list_articles()

    if not articles:
        print("No articles found in index.")
        print("Add articles to data/article_modifications.json")
        return

    print(f"\nArticles disponibles ({len(articles)}):\n")
    for ref in sorted(articles):
        info = index.get_article(ref)
        if info:
            mod_count = len(info.modifications)
            print(f"  {ref:<25} {info.titre[:70]:<72} ({mod_count} mod.)")


def check_prerequisites() -> bool:
    """Check that prerequisites are met."""
    from src.utils.config import get_settings

    settings = get_settings()

    # Check LLM provider
    if settings.llm_provider == "groq" and not settings.get_groq_api_keys():
        print("ERREUR: GROQ_API_KEY(S) non configurée(s)")
        print("Configurez GROQ_API_KEYS dans .env ou utilisez LLM_PROVIDER=anthropic")
        return False
    elif settings.llm_provider == "anthropic" and not settings.anthropic_api_key:
        print("ERREUR: ANTHROPIC_API_KEY non configurée")
        print("Configurez ANTHROPIC_API_KEY dans .env ou utilisez LLM_PROVIDER=groq")
        return False

    return True


def _build_search_service(db_url: str):
    """
    Build a SearchService backed by pgvector on the production DB.

    Uses a single ChunkEmbeddingsPgVectorStore for both debate and amendment
    stores — chunk_type filtering is handled in SQL.
    """
    from src.indexing.search import SearchService
    from src.indexing.vector_stores import ChunkEmbeddingsPgVectorStore

    print(f"Connexion pgvector → {db_url[:50]}…")

    pgvector_store = ChunkEmbeddingsPgVectorStore(
        connection_string=db_url,
        embedding_dim=384,
    )

    # Quick connectivity check — count is expensive on 3M rows, use estimate
    import psycopg2
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT reltuples::bigint FROM pg_class WHERE relname = 'chunk_embeddings'"
        )
        row = cur.fetchone()
        total = row[0] if row else 0
        # Get actual counts per chunk_type from stats
        cur.execute(
            "SELECT chunk_type, COUNT(*) FROM chunk_embeddings "
            "GROUP BY chunk_type"
        )
        type_counts = dict(cur.fetchall())
    conn.close()

    debates = type_counts.get("intervention", 0)
    amendments = type_counts.get("amendement", 0)
    print(f"  Chunks totaux    : {total:>12,}")
    print(f"  Débats           : {debates:>12,}")
    print(f"  Amendements      : {amendments:>12,}")

    # The SearchService expects separate stores but the pgvector store
    # handles chunk_type filtering internally via SQL. We pass the same
    # store instance for both so hybrid_search routes through the
    # _hybrid_search_external_store path with chunk_type="intervention"
    # for debates and chunk_type="amendement" for amendments.
    search_service = SearchService(
        debate_store=pgvector_store,
        amendment_store=pgvector_store,
    )

    return search_service


def analyze_article(
    article_ref: str,
    db_url: str,
    question: str | None = None,
    output_path: Path | None = None,
    max_modifications: int | None = None,
    include_consolidation: bool = True,
):
    """
    Analyze an article and generate markdown document.

    Args:
        article_ref: Article reference (e.g., "150-0 B ter")
        db_url: PostgreSQL connection string
        question: Optional question to answer
        output_path: Output file path (auto-generated if None)
        max_modifications: Limit number of modifications to analyze
        include_consolidation: Whether to include consolidation periods
    """
    from src.analysis.article_analyzer import ArticleAnalyzer
    from src.analysis.article_index import get_article_index
    from src.analysis.renderer import render_article_analysis

    # Default question
    if not question:
        question = (
            f"Pourquoi le législateur a-t-il créé et modifié l'article {article_ref} "
            f"du CGI ? Quelle est l'intention du législateur ?"
        )

    # Check prerequisites
    if not check_prerequisites():
        return False

    # Check article exists
    index = get_article_index()
    article_info = index.get_article(article_ref)
    if not article_info:
        print(f"\nERREUR: Article '{article_ref}' non trouvé dans l'index.")
        print("Articles disponibles :")
        for ref in sorted(index.list_articles()):
            print(f"  - {ref}")
        print("\nPour ajouter un article, éditez data/article_modifications.json")
        return False

    mod_count = len(article_info.modifications)
    if max_modifications:
        mod_count = min(mod_count, max_modifications)
    print(f"\n{'='*70}")
    print(f"  Article : {article_ref} ({article_info.titre})")
    print(f"  Modifications à analyser : {mod_count}")
    print(f"  Question : {question}")
    print(f"{'='*70}\n")

    # Build search service with pgvector
    search_service = _build_search_service(db_url)

    # Create analyzer
    analyzer = ArticleAnalyzer(search_service=search_service)

    # Run analysis
    print(f"\nAnalyse en cours…\n")
    try:
        result = analyzer.analyze_article(
            article_ref=article_ref,
            question=question,
            include_jurisprudence=True,
            max_modifications=max_modifications,
            include_consolidation_periods=include_consolidation,
        )
    except Exception as e:
        print(f"ERREUR: Analyse échouée: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Render to markdown
    markdown = render_article_analysis(result)

    # Determine output path
    if output_path is None:
        safe_ref = article_ref.replace(" ", "_").replace("-", "_")
        output_dir = project_root / "data" / "analyses"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"analyse_{safe_ref}.md"

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(markdown)

    # Print summary
    print(f"\n{'='*70}")
    print(f"  Analyse terminée !")
    print(f"{'='*70}")
    print(f"  Fichier        : {output_path}")
    print(f"  Modifications  : {len(result.sections)}")
    print(f"  Débats analysés: {result.total_debates_searched}")
    print(f"  Jurisprudence  : {len(result.jurisprudence)}")

    # Quality summary
    eleve = sum(
        1 for s in result.sections
        if s.niveau_certitude.value == "élevé"
    )
    moyen = sum(
        1 for s in result.sections
        if s.niveau_certitude.value == "moyen"
    )
    faible = sum(
        1 for s in result.sections
        if s.niveau_certitude.value == "faible"
    )
    print(f"  Certitude      : {eleve} élevé / {moyen} moyen / {faible} faible")

    total_sources = sum(
        len(s.sources_an) + len(s.sources_senat) for s in result.sections
    )
    print(f"  Sources totales: {total_sources}")
    if result.consolidation_periods:
        print(f"  Périodes consolidation: {len(result.consolidation_periods)}")
    if result.forward_looking:
        print(f"  Forward-looking (PLF) : {len(result.forward_looking)}")
    print(f"{'='*70}\n")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Analyse d'article CGI via pgvector (PostgreSQL)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples:
  %(prog)s "150-0 B ter"
  %(prog)s "787 B" --question "Quelles sont les conditions du Dutreil?"
  %(prog)s "150 U" --output rapport.md
  %(prog)s "150-0 B ter" --max-modifications 3
  %(prog)s --list
        """,
    )

    parser.add_argument(
        "article",
        nargs="?",
        help="Référence de l'article CGI (ex: '150-0 B ter', '787 B')",
    )
    parser.add_argument(
        "-q", "--question",
        help="Question à laquelle répondre sur l'article",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        help="Chemin du fichier de sortie (défaut: data/analyses/analyse_<article>.md)",
    )
    parser.add_argument(
        "-m", "--max-modifications",
        type=int,
        help="Limiter le nombre de modifications à analyser",
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="Lister les articles disponibles dans l'index",
    )
    parser.add_argument(
        "--db-url",
        default=DEFAULT_DATABASE_URL,
        help="URL de connexion PostgreSQL (défaut: $DATABASE_URL)",
    )
    parser.add_argument(
        "--no-consolidation",
        action="store_true",
        help="Ne pas inclure les périodes de consolidation",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Activer les logs détaillés",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.db_url:
        parser.error("DATABASE_URL non défini : exportez-le ou passez --db-url")

    # Apply DB URL before any imports that use settings
    _apply_db_url(args.db_url)

    if args.list:
        list_articles()
        return 0

    if not args.article:
        parser.print_help()
        print("\nERREUR: Veuillez fournir une référence d'article ou utiliser --list")
        return 1

    success = analyze_article(
        article_ref=args.article,
        db_url=args.db_url,
        question=args.question,
        output_path=args.output,
        max_modifications=args.max_modifications,
        include_consolidation=not args.no_consolidation,
    )

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
