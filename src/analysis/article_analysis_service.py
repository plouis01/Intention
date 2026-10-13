"""
Article analysis service for API usage.

Runs the same analysis as scripts/analyse_article.py but persists results
to the DB cache (AnalysisCache) instead of writing a markdown file.
Checks cache before running to avoid redundant LLM calls.
"""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from src.analysis.article_analyzer import ArticleAnalyzer
from src.analysis.article_index import get_article_index
from src.analysis.models import ArticleAnalysisResult
from src.db.models import AnalysisCache, get_session
from src.indexing.search import SearchService
from src.utils.config import get_settings

logger = logging.getLogger(__name__)

_project_root = Path(__file__).parent.parent.parent


def normalize_article_ref(article_ref: str) -> str:
    """Normalize article reference using the ArticleIndex."""
    cleaned = article_ref.replace("_", " ")
    index = get_article_index()
    for ref in index.list_articles():
        if ref.lower() == cleaned.lower():
            return ref
    return cleaned


def _compute_current_hash(cgi_article: str) -> str:
    """Compute the current modifications hash for an article from the index file."""
    index_path = _project_root / "data" / "article_modifications.json"
    if not index_path.exists():
        return ""
    try:
        with open(index_path, encoding="utf-8") as f:
            raw_index = json.load(f)
        for ref, data in raw_index.get("articles", {}).items():
            if ref.lower() == cgi_article.lower():
                return _compute_modifications_hash(data.get("modifications", []))
    except Exception:
        pass
    return ""


def get_cached_analysis(cgi_article: str) -> dict | None:
    """Get cached analysis from DB if available, not expired, and not stale."""
    try:
        with get_session() as session:
            cache = (
                session.query(AnalysisCache)
                .filter(AnalysisCache.cgi_article == cgi_article)
                .first()
            )
            if not cache:
                return None
            if cache.expires_at:
                expires = cache.expires_at.replace(tzinfo=None)
                now = datetime.now(UTC).replace(tzinfo=None)
                if expires < now:
                    session.delete(cache)
                    session.commit()
                    return None
            # Invalidate if article_modifications.json has changed since caching
            current_hash = _compute_current_hash(cgi_article)
            if current_hash and cache.modifications_hash != current_hash:
                session.delete(cache)
                session.commit()
                logger.info(f"Stale cache for {cgi_article}, invalidated")
                return None
            return cache.analysis_json
    except Exception as e:
        logger.warning(f"Error getting cached analysis: {e}")
        return None


def _compute_modifications_hash(modifications: list[dict]) -> str:
    sorted_mods = sorted(modifications, key=lambda m: (m.get("date", ""), m.get("law_numero", "")))
    stable_json = json.dumps(sorted_mods, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(stable_json.encode("utf-8")).hexdigest()


def _serialize_quoted_source(source) -> dict:
    return {
        "citation": source.citation,
        "orateur": source.orateur,
        "fonction": source.fonction,
        "date": source.date,
        "chambre": source.chambre,
    }


def _serialize_section(section, index: int) -> dict:
    mod = section.modification
    return {
        "index": index,
        "modification": {
            "date": mod.date.isoformat(),
            "law_numero": mod.law_numero,
            "law_type": mod.law_type,
            "law_year": mod.law_year,
            "article_in_law": mod.article_in_law,
            "nature": mod.nature.value,
            "law_ref": mod.law_ref,
        },
        "contexte": section.contexte,
        "intention": section.intention,
        "sources_an": [_serialize_quoted_source(s) for s in section.sources_an],
        "sources_senat": [_serialize_quoted_source(s) for s in section.sources_senat],
        "evolution_debats": section.evolution_debats,
        "points_vigilance": section.points_vigilance,
        "niveau_certitude": section.niveau_certitude.value,
        "certitude_justification": section.certitude_justification,
        "debates_analyzed": section.debates_analyzed,
        "amendments_analyzed": section.amendments_analyzed,
        "operational_params": section.operational_params.parameters if section.operational_params else None,
    }


def _result_to_dict(result: ArticleAnalysisResult, article_ref: str, question: str) -> dict:
    """Convert ArticleAnalysisResult to the same dict structure used by the stream cache."""
    sections = [_serialize_section(s, i) for i, s in enumerate(result.sections)]

    consolidation = [
        {
            "title": cp.title,
            "doctrine_evolution": cp.doctrine_evolution,
            "amendements_rejetes": cp.amendements_rejetes,
            "citations": [_serialize_quoted_source(c) for c in cp.citations],
        }
        for cp in result.consolidation_periods
    ]

    synthesis = {
        "objectif": result.synthese_objectif,
        "evolution": [
            {"periode": row.periode, **row.parameters}
            for row in result.synthese_evolution
        ],
        "preoccupations": result.synthese_preoccupations,
    }

    forward_looking = [
        {
            "plf_year": fl.plf_year,
            "status": fl.status,
            "propositions": fl.propositions,
            "citations": [_serialize_quoted_source(c) for c in fl.citations],
            "niveau_certitude": fl.niveau_certitude.value,
        }
        for fl in result.forward_looking
    ]

    jurisprudence = [
        {
            "juridiction": j.juridiction,
            "date": j.date,
            "reference": j.reference,
            "titre": j.titre,
            "faits": j.faits,
            "solution": j.solution,
            "portee": j.portee,
        }
        for j in result.jurisprudence
    ]

    # Read raw modifications from article_modifications.json for hash/invalidation
    index_path = _project_root / "data" / "article_modifications.json"
    raw_modifications = []
    if index_path.exists():
        with open(index_path, encoding="utf-8") as f:
            raw_index = json.load(f)
        for ref, data in raw_index.get("articles", {}).items():
            if ref.lower() == article_ref.lower():
                raw_modifications = data.get("modifications", [])
                break

    settings = get_settings()
    llm_model = settings.groq_model if settings.llm_provider == "groq" else settings.claude_model

    return {
        "article_info": {
            "reference": result.article_ref,
            "titre": result.article_titre,
            "code": result.article_code,
            "question": question,
            "total_modifications": len(result.historique),
            "total_sections": len(sections),
            "modifications": [
                {"date": m.date.isoformat(), "law_ref": m.law_ref, "nature": m.nature.value}
                for m in result.historique
            ],
        },
        "sections": sections,
        "consolidation": consolidation,
        "synthesis": synthesis,
        "forward_looking": forward_looking,
        "jurisprudence": jurisprudence,
        "cross_references": result.cross_references,
        "plf_lf_deltas": result.plf_lf_deltas,
        "rapports_parlementaires": result.rapports_parlementaires,
        "dila_sources": result.dila_sources,
        "sources_parlementaires_an": result.sources_parlementaires_an,
        "sources_parlementaires_senat": result.sources_parlementaires_senat,
        "metadata": {
            "total_sections": len(result.sections),
            "total_debates_searched": result.total_debates_searched,
            "total_interventions_found": result.total_interventions_found,
            "generated_date": result.generated_date,
            "llm_model": llm_model,
            "embedding_model": settings.local_embedding_model,
        },
        "invalidation": {
            "modification_count": len(raw_modifications),
            "last_modification_date": raw_modifications[-1]["date"] if raw_modifications else "",
            "modifications_hash": _compute_modifications_hash(raw_modifications),
        },
    }


def _save_to_cache(cgi_article: str, analysis: dict) -> None:
    """Persist analysis dict to AnalysisCache DB table."""
    inv = analysis.get("invalidation", {})
    mod_hash = inv.get("modifications_hash", "")
    modification_count = inv.get("modification_count", 0)
    last_mod_date = inv.get("last_modification_date", "")

    certitude_scores = {"élevé": 1.0, "moyen": 0.5, "faible": 0.0}
    section_scores = []
    total_sources = 0
    min_certitude = "élevé"
    for s in analysis.get("sections", []):
        certitude = s.get("niveau_certitude", "moyen")
        score = certitude_scores.get(certitude, 0.5)
        source_count = len(s.get("sources_an", [])) + len(s.get("sources_senat", []))
        section_scores.append(score)
        total_sources += source_count
        if score < certitude_scores.get(min_certitude, 1.0):
            min_certitude = certitude

    overall_quality = sum(section_scores) / len(section_scores) if section_scores else 0.0

    settings = get_settings()
    llm_model = settings.groq_model if settings.llm_provider == "groq" else settings.claude_model
    now = datetime.now(UTC).replace(tzinfo=None)
    expires = now + timedelta(days=30)

    with get_session() as session:
        try:
            existing = (
                session.query(AnalysisCache)
                .filter(AnalysisCache.cgi_article == cgi_article)
                .first()
            )

            def _update(row):
                row.analysis_json = analysis
                row.modifications_hash = mod_hash
                row.modification_count = modification_count
                row.last_modification_date = last_mod_date
                row.quality_score = int(overall_quality * 100)
                row.sources_count = total_sources
                row.confidence_level = min_certitude
                row.created_at = now
                row.expires_at = expires
                row.llm_model = llm_model

            if existing:
                _update(existing)
                session.commit()
            else:
                try:
                    cache = AnalysisCache(
                        cgi_article=cgi_article,
                        analysis_json=analysis,
                        modifications_hash=mod_hash,
                        modification_count=modification_count,
                        last_modification_date=last_mod_date,
                        quality_score=int(overall_quality * 100),
                        sources_count=total_sources,
                        confidence_level=min_certitude,
                        created_at=now,
                        expires_at=expires,
                        llm_model=llm_model,
                    )
                    session.add(cache)
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    existing = (
                        session.query(AnalysisCache)
                        .filter(AnalysisCache.cgi_article == cgi_article)
                        .first()
                    )
                    if existing:
                        _update(existing)
                        session.commit()

            logger.info(f"Cached analysis for {cgi_article}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error saving cache for {cgi_article}: {e}")
            raise


def analyze_article_for_api(
    article_ref: str,
    question: str | None = None,
    max_modifications: int | None = None,
    search_service: "SearchService | None" = None,
) -> dict:
    """
    Run article analysis and return result as dict (same structure as stream cache).

    Checks DB cache first — returns cached result if available and not expired.
    Otherwise runs full analysis via ArticleAnalyzer and persists to cache.

    Args:
        article_ref: Article reference (e.g., "150-0 B ter")
        question: Optional question (default: standard legislative intent question)
        max_modifications: Limit number of modifications to analyze
        search_service: Optional pre-initialized SearchService (e.g. from API startup).
            If None, loads embeddings from disk (CLI fallback).

    Returns:
        Analysis dict with keys: article_info, sections, consolidation, synthesis,
        forward_looking, jurisprudence, cross_references, plf_lf_deltas,
        rapports_parlementaires, dila_sources, sources_parlementaires_an/senat,
        metadata, invalidation, from_cache
    """
    article_ref_normalized = normalize_article_ref(article_ref)

    if not question:
        question = f"Pourquoi le législateur a-t-il créé et modifié l'article {article_ref_normalized} ?"

    cached = get_cached_analysis(article_ref_normalized)
    if cached:
        logger.info(f"Returning cached analysis for {article_ref_normalized}")
        cached["from_cache"] = True
        return cached

    index = get_article_index()
    if not index.get_article(article_ref_normalized):
        raise ValueError(
            f"Article '{article_ref_normalized}' not found in index. "
            f"Available: {index.list_articles()}"
        )

    if search_service is None:
        logger.info("Loading vector stores...")
        search_service = SearchService()
        embeddings_dir = _project_root / "data" / "embeddings"
        search_service.load_stores(embeddings_dir)

    analyzer = ArticleAnalyzer(search_service=search_service)

    logger.info(f"Analyzing {article_ref_normalized}...")
    result = analyzer.analyze_article(
        article_ref=article_ref_normalized,
        question=question,
        include_jurisprudence=True,
        max_modifications=max_modifications,
    )

    analysis = _result_to_dict(result, article_ref_normalized, question)
    _save_to_cache(article_ref_normalized, analysis)
    analysis["from_cache"] = False

    return analysis
