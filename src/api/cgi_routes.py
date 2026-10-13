"""API routes for CGI article endpoints with caching and streaming.

Railway production deployment:
  1. Backup DB via Railway dashboard
  2. Run scripts/migrate_to_pgvector_safe.sql (~2h30 for 1.6M chunks)
  3. Monitor: SELECT * FROM pg_stat_progress_create_index;
  4. Set env: VECTOR_STORE_PROVIDER=pgvector
  5. Redeploy — config.py handles postgres:// → postgresql:// conversion
  6. See docs/pgvector-next-steps.md for details
"""

import asyncio
import dataclasses
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from itertools import groupby
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from src.analysis.article_analyzer import ArticleAnalyzer
from src.analysis.article_index import get_article_index as get_analysis_article_index
from src.analysis.cross_law_analyzer import CrossLawAnalyzer
from src.api.routes import get_search_service
from src.api.schemas import (
    CGIArticleDetail,
    CGIArticleListResponse,
    CGIArticleSummary,
    CGIModification,
    ErrorResponse,
)
from sqlalchemy.exc import IntegrityError

from src.db.models import AnalysisCache, get_session
from src.utils.config import get_settings

logger = logging.getLogger(__name__)

# Create router
cgi_router = APIRouter(prefix="/cgi", tags=["cgi"])

# Global services
_article_index: dict | None = None
_article_index_mtime: float = 0.0


def get_article_index() -> dict:
    """Load and cache the article_modifications.json index, auto-reloading on file change."""
    global _article_index, _article_index_mtime

    if _article_index is not None and _article_index_mtime == 0.0:
        # Set externally (e.g., by tests) — don't override
        return _article_index

    index_path = Path("data/article_modifications.json")

    if index_path.exists():
        current_mtime = index_path.stat().st_mtime
        if _article_index is None or current_mtime != _article_index_mtime:
            with open(index_path, encoding="utf-8") as f:
                _article_index = json.load(f)
            _article_index_mtime = current_mtime
            logger.info(f"Loaded {len(_article_index.get('articles', {}))} CGI articles from index")
    elif _article_index is None:
        logger.warning("article_modifications.json not found")
        _article_index = {"articles": {}}

    return _article_index


def normalize_article_ref(article_ref: str) -> str:
    """Normalize article reference to canonical form from the index.

    Applies underscore→space conversion and case-insensitive lookup.
    Returns the canonical reference from article_modifications.json
    if found, otherwise the cleaned input.
    """
    cleaned = article_ref.replace("_", " ")
    index = get_article_index()
    for ref in index.get("articles", {}):
        if ref.lower() == cleaned.lower():
            return ref
    return cleaned


# ============================================================================
# Caching Functions
# ============================================================================


def compute_modifications_hash(modifications: list[dict]) -> str:
    """Compute deterministic hash of modifications list for cache invalidation."""
    sorted_mods = sorted(modifications, key=lambda m: (m.get("date", ""), m.get("law_numero", "")))
    stable_json = json.dumps(sorted_mods, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(stable_json.encode("utf-8")).hexdigest()


def _compute_current_hash(cgi_article: str) -> str:
    """Compute the current modifications hash for an article from the index."""
    index = get_article_index()
    for ref, data in index.get("articles", {}).items():
        if ref.lower() == cgi_article.lower():
            return compute_modifications_hash(data.get("modifications", []))
    return ""


def is_analysis_stale(cgi_article: str) -> tuple[bool, str]:
    """Check if cached analysis is stale vs current article_modifications.json.

    Returns:
        (is_stale, current_hash) — True if stale or no cache exists.
    """
    current_hash = _compute_current_hash(cgi_article)
    if not current_hash:
        return True, ""

    try:
        with get_session() as session:
            cache = (
                session.query(AnalysisCache)
                .filter(AnalysisCache.cgi_article == cgi_article)
                .first()
            )
            if not cache:
                return True, current_hash
            if cache.modifications_hash != current_hash:
                return True, current_hash
            return False, current_hash
    except Exception as e:
        logger.warning(f"Error checking staleness: {e}")
        return True, current_hash


def get_cached_analysis(cgi_article: str) -> dict | None:
    """Get cached analysis if available, not expired, and not stale."""
    try:
        with get_session() as session:
            cache = (
                session.query(AnalysisCache)
                .filter(AnalysisCache.cgi_article == cgi_article)
                .first()
            )

            if not cache:
                return None

            # Check expiration (compare as naive UTC — DB column has no timezone)
            if cache.expires_at:
                expires = cache.expires_at.replace(tzinfo=None)
                now = datetime.now(UTC).replace(tzinfo=None)
                if expires < now:
                    session.delete(cache)
                    session.commit()
                    return None

            # Check invalidation inline (avoid second DB query)
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


def save_cached_analysis(
    cgi_article: str,
    analysis: dict,
    modifications_hash: str,
    modification_count: int,
    last_modification_date: str,
    quality_score: float,
    sources_count: int,
    confidence_level: str,
    ttl_days: int = 30,
) -> None:
    """Save consolidated analysis to cache (one entry per CGI article)."""
    with get_session() as session:
        try:
            settings = get_settings()

            existing = (
                session.query(AnalysisCache)
                .filter(AnalysisCache.cgi_article == cgi_article)
                .first()
            )

            # Store as naive UTC (DB column has no timezone info)
            now = datetime.now(UTC).replace(tzinfo=None)
            expires = now + timedelta(days=ttl_days)
            llm_model = (
                settings.groq_model
                if settings.llm_provider == "groq"
                else settings.claude_model
            )

            def _update_existing(row):
                row.analysis_json = analysis
                row.modifications_hash = modifications_hash
                row.modification_count = modification_count
                row.last_modification_date = last_modification_date
                row.quality_score = int(quality_score * 100)
                row.sources_count = sources_count
                row.confidence_level = confidence_level
                row.created_at = now
                row.expires_at = expires
                row.llm_model = llm_model

            if existing:
                _update_existing(existing)
                session.commit()
            else:
                try:
                    cache = AnalysisCache(
                        cgi_article=cgi_article,
                        analysis_json=analysis,
                        modifications_hash=modifications_hash,
                        modification_count=modification_count,
                        last_modification_date=last_modification_date,
                        quality_score=int(quality_score * 100),
                        sources_count=sources_count,
                        confidence_level=confidence_level,
                        created_at=now,
                        expires_at=expires,
                        llm_model=llm_model,
                    )
                    session.add(cache)
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    # Another request inserted concurrently — update instead
                    existing = (
                        session.query(AnalysisCache)
                        .filter(AnalysisCache.cgi_article == cgi_article)
                        .first()
                    )
                    if existing:
                        _update_existing(existing)
                        session.commit()

            logger.info(f"Cached consolidated analysis for {cgi_article}")

        except Exception as e:
            session.rollback()
            logger.warning(f"Error saving cached analysis: {e}")
            raise


# ============================================================================
# CGI Article Endpoints
# ============================================================================


@cgi_router.get(
    "",
    response_model=CGIArticleListResponse,
    summary="List all CGI articles",
    description="Get a list of all CGI articles that have been modified by finance laws.",
)
async def list_cgi_articles(
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of results"),
    offset: int = Query(0, ge=0, description="Number of results to skip"),
    min_modifications: int = Query(0, ge=0, description="Minimum number of modifications"),
    search: str | None = Query(None, description="Search in article reference or title"),
):
    """List all CGI articles with modification counts."""
    try:
        index = get_article_index()
        articles_data = index.get("articles", {})

        # Filter and build response
        articles = []
        for ref, data in articles_data.items():
            mods = data.get("modifications", [])
            mod_count = len(mods)

            # Apply filters
            if mod_count < min_modifications:
                continue

            if search:
                search_lower = search.lower()
                if search_lower not in ref.lower() and search_lower not in data.get("titre", "").lower():
                    continue

            last_mod = mods[-1]["date"] if mods else None

            articles.append(
                CGIArticleSummary(
                    reference=ref,
                    titre=data.get("titre", ""),
                    code=data.get("code", "CGI"),
                    modification_count=mod_count,
                    last_modified=last_mod,
                )
            )

        # Sort by modification count (descending)
        articles.sort(key=lambda x: x.modification_count, reverse=True)

        # Apply pagination
        total = len(articles)
        articles = articles[offset : offset + limit]

        return CGIArticleListResponse(total=total, articles=articles)

    except Exception as e:
        logger.exception("Error listing CGI articles")
        raise HTTPException(status_code=500, detail=str(e))


@cgi_router.get(
    "/cached-refs",
    summary="List cached article refs",
    description="Returns the list of CGI article references that have a valid (non-expired, non-stale) cached analysis.",
)
async def list_cached_refs() -> JSONResponse:
    """Return the set of CGI article refs with a valid cached analysis."""
    try:
        now = datetime.now(UTC).replace(tzinfo=None)
        with get_session() as session:
            rows = (
                session.query(AnalysisCache.cgi_article, AnalysisCache.expires_at, AnalysisCache.modifications_hash)
                .all()
            )

        cached_refs = []
        for cgi_article, expires_at, modifications_hash in rows:
            # Skip expired entries
            if expires_at:
                expires = expires_at.replace(tzinfo=None)
                if expires < now:
                    continue
            # Skip stale entries (modifications changed since cache was built)
            current_hash = _compute_current_hash(cgi_article)
            if current_hash and modifications_hash != current_hash:
                continue
            cached_refs.append(cgi_article)

        return JSONResponse({"refs": cached_refs})

    except Exception as e:
        logger.exception("Error listing cached refs")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Serialization Helpers (for analysis dataclasses → JSON)
# ============================================================================


def _serialize_quoted_source(source) -> dict:
    """Serialize a QuotedSource dataclass to a JSON-serializable dict."""
    return {
        "citation": source.citation,
        "orateur": source.orateur,
        "fonction": source.fonction,
        "date": source.date,
        "chambre": source.chambre,
    }


def _serialize_section(section, index: int) -> dict:
    """Serialize a SectionAnalysis dataclass to a JSON-serializable dict."""
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


# ============================================================================
# Streaming Endpoint
# ============================================================================


@dataclasses.dataclass
class _RunningAnalysis:
    buffer: list[str] = dataclasses.field(default_factory=list)
    subscribers: list[asyncio.Queue] = dataclasses.field(default_factory=list)
    done: bool = False


# key: "{article_ref}::{question}::{max_modifications}" → ongoing analysis
_running_analyses: dict[str, _RunningAnalysis] = {}


async def _run_analysis_task(
    article_ref: str,
    question: str,
    max_modifications: int | None,
    running: _RunningAnalysis,
    analysis_key: str,
) -> None:
    """Background task: runs the analysis and fan-outs events. Lifecycle is independent of HTTP connections."""
    try:
        async for event in _run_analysis(article_ref, question, max_modifications):
            running.buffer.append(event)
            dead: list[asyncio.Queue] = []
            for q in list(running.subscribers):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                try:
                    running.subscribers.remove(q)
                except ValueError:
                    pass
    finally:
        running.done = True
        for q in list(running.subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        _running_analyses.pop(analysis_key, None)


async def _stream_from_running(running: _RunningAnalysis, request: Request | None = None) -> AsyncGenerator[str, None]:
    """HTTP generator: replays buffer then listens to queue. Disconnecting doesn't affect the analysis task."""
    # If already done: replay full buffer and return — None sentinel already sent, no queue needed
    if running.done:
        buffer_snapshot = list(running.buffer)
        logger.debug(f"[stream] done=True at entry, replaying {len(buffer_snapshot)} buffered events")
        for event in buffer_snapshot:
            yield event
            await asyncio.sleep(0)
        return

    # Analysis still running: snapshot buffer + subscribe atomically
    # (asyncio single-threaded — no await between snapshot and append)
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    buffered = list(running.buffer)
    running.subscribers.append(q)

    # Re-check done AFTER subscribing: the task's finally block may have run between
    # our initial done-check above and this append — if so, the None sentinel was
    # dispatched before our queue was registered and will never arrive.
    # Fall back to full buffer replay and return without waiting on the queue.
    if running.done:
        try:
            running.subscribers.remove(q)
        except ValueError:
            pass
        buffer_snapshot = list(running.buffer)
        logger.debug(f"[stream] done=True after subscribe race, replaying {len(buffer_snapshot)} buffered events")
        for event in buffer_snapshot:
            yield event
            await asyncio.sleep(0)
        return

    try:
        for event in buffered:
            yield event
            await asyncio.sleep(0)
        # After replaying buffer, listen for live events.
        # Use a short timeout on q.get() so we can:
        # 1. Detect client disconnection (via request.is_disconnected())
        # 2. Yield keepalive pings so Starlette can detect socket closure
        # Without this, a disconnected client holds an HTTP connection open
        # until the next analysis event (up to 50+ seconds), starving Chrome's
        # 6-connection-per-hostname limit.
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=2.0)
            except asyncio.TimeoutError:
                if request and await request.is_disconnected():
                    logger.debug("[stream] Client disconnected, closing subscriber")
                    break
                yield json.dumps({"type": "ping"}) + "\n"
                continue
            if event is None:
                break
            yield event
    finally:
        try:
            running.subscribers.remove(q)
        except ValueError:
            pass


async def _run_analysis(
    article_ref: str,
    question: str,
    max_modifications: int | None,
) -> AsyncGenerator[str, None]:
    """Inner generator: actual analysis logic (no fan-out concerns)."""
    try:
        yield json.dumps({"type": "status", "data": "Initializing analysis..."}) + "\n"

        # Collector for persistence
        collected: dict = {
            "article_info": None,
            "sections": [],
            "consolidation": [],
            "synthesis": None,
            "forward_looking": [],
            "jurisprudence": [],
            "cross_references": [],
            "plf_lf_deltas": [],
            "rapports_parlementaires": [],
            "dila_sources": [],
            "sources_parlementaires_an": [],
            "sources_parlementaires_senat": [],
            "metadata": {},
        }

        # Initialize analyzer with configured search service (uses pgvector if configured)
        search_service = get_search_service()
        analyzer = ArticleAnalyzer(search_service)

        # Resolve article
        article_info = analyzer.article_index.get_article(article_ref)
        if not article_info:
            yield json.dumps({"type": "error", "data": f"Article '{article_ref}' not found"}) + "\n"
            return

        modifications = article_info.modifications
        if max_modifications:
            modifications = modifications[:max_modifications]

        # Group modifications by law_numero (single search per law)
        # Sort first — groupby only groups consecutive elements
        sorted_mods = sorted(modifications, key=lambda m: m.law_numero)
        grouped_mods: list[list] = []
        for _key, group in groupby(sorted_mods, key=lambda m: m.law_numero):
            grouped_mods.append(list(group))
        total_groups = len(grouped_mods)

        # Yield article info
        article_info_data = {
            "reference": article_info.reference,
            "titre": article_info.titre,
            "code": article_info.code,
            "question": question,
            "total_modifications": len(modifications),
            "total_sections": total_groups,
            "modifications": [
                {"date": m.date.isoformat(), "law_ref": m.law_ref, "nature": m.nature.value}
                for m in modifications
            ],
        }
        yield json.dumps({"type": "article_info", "data": article_info_data}) + "\n"
        collected["article_info"] = article_info_data

        # Ensure search service is loaded (can take 1-2 min on cold start — send keepalive pings)
        yield json.dumps({"type": "status", "data": "Loading search service..."}) + "\n"
        _task = asyncio.create_task(asyncio.to_thread(analyzer._ensure_search_service))
        while not _task.done():
            try:
                await asyncio.wait_for(asyncio.shield(_task), timeout=30.0)
            except asyncio.TimeoutError:
                yield json.dumps({"type": "ping"}) + "\n"
        _task.result()

        # Analyze each modification group
        sections = []
        total_debates = 0
        total_interventions = 0
        used_curated_citations: set[str] = set()

        for i, mod_group in enumerate(grouped_mods):
            primary_mod = mod_group[0]
            yield json.dumps({
                "type": "status",
                "data": f"Analyzing {primary_mod.short_ref} ({i + 1}/{total_groups})...",
            }) + "\n"

            try:
                _task = asyncio.create_task(
                    asyncio.to_thread(analyzer._analyze_modification, article_info, primary_mod)
                )
                while not _task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(_task), timeout=30.0)
                    except asyncio.TimeoutError:
                        yield json.dumps({"type": "ping"}) + "\n"
                section, debates_count, interventions_count = _task.result()

                # Augment with curated quotes (same as sync path)
                section = await asyncio.to_thread(
                    analyzer._augment_with_curated_quotes,
                    article_info.reference, primary_mod, section,
                    article_keywords=article_info.keywords,
                    used_curated_citations=used_curated_citations,
                )

                # Merge all articles from the group into the section
                if len(mod_group) > 1:
                    all_articles = ", ".join(m.article_in_law for m in mod_group)
                    section.modification = dataclasses.replace(
                        primary_mod, article_in_law=all_articles
                    )

                section_data = _serialize_section(section, i)
                yield json.dumps({"type": "section", "data": section_data}) + "\n"
                collected["sections"].append(section_data)

                sections.append(section)
                total_debates += debates_count
                total_interventions += interventions_count

            except Exception as section_err:
                logger.exception(f"Error analyzing {primary_mod.short_ref}")
                yield json.dumps({
                    "type": "section_error",
                    "data": {
                        "index": i,
                        "law_ref": primary_mod.law_ref,
                        "error": str(section_err),
                    },
                }) + "\n"

        # Consolidation periods
        yield json.dumps({"type": "status", "data": "Detecting consolidation periods..."}) + "\n"

        _task = asyncio.create_task(
            asyncio.to_thread(analyzer._detect_consolidation_periods, article_info, modifications)
        )
        while not _task.done():
            try:
                await asyncio.wait_for(asyncio.shield(_task), timeout=30.0)
            except asyncio.TimeoutError:
                yield json.dumps({"type": "ping"}) + "\n"
        try:
            consolidation_periods = _task.result()
        except Exception as consolidation_err:
            logger.exception("Error detecting consolidation periods")
            yield json.dumps({"type": "error", "data": f"Consolidation detection failed: {consolidation_err}"}) + "\n"
            consolidation_periods = []
        for cp in consolidation_periods:
            cp_data = {
                "start_year": cp.start_year,
                "end_year": cp.end_year,
                "title": cp.title,
                "doctrine_evolution": cp.doctrine_evolution,
                "amendements_rejetes": cp.amendements_rejetes,
                "citations": [_serialize_quoted_source(c) for c in cp.citations],
            }
            yield json.dumps({"type": "consolidation", "data": cp_data}) + "\n"
            collected["consolidation"].append(cp_data)

        # Synthesis
        yield json.dumps({"type": "status", "data": "Building synthesis..."}) + "\n"

        _task = asyncio.create_task(asyncio.to_thread(analyzer._build_synthesis, article_info, sections))
        while not _task.done():
            try:
                await asyncio.wait_for(asyncio.shield(_task), timeout=30.0)
            except asyncio.TimeoutError:
                yield json.dumps({"type": "ping"}) + "\n"
        try:
            synthese = _task.result()
        except Exception as synthesis_err:
            logger.exception("Error building synthesis")
            yield json.dumps({"type": "error", "data": f"Synthesis failed: {synthesis_err}"}) + "\n"
            synthese = {"objectif": "", "evolution": [], "preoccupations": []}
        synthesis_data = {
            "objectif": synthese.get("objectif", ""),
            "evolution": [
                {"periode": r.periode, **r.parameters}
                for r in synthese.get("evolution", [])
            ],
            "preoccupations": synthese.get("preoccupations", []),
        }
        yield json.dumps({"type": "synthesis", "data": synthesis_data}) + "\n"
        collected["synthesis"] = synthesis_data

        # Forward-looking
        _task = asyncio.create_task(asyncio.to_thread(analyzer._get_forward_looking, article_info))
        while not _task.done():
            try:
                await asyncio.wait_for(asyncio.shield(_task), timeout=30.0)
            except asyncio.TimeoutError:
                yield json.dumps({"type": "ping"}) + "\n"
        forward_looking = _task.result()
        if forward_looking:
            fl_data = [
                {
                    "plf_year": fl.plf_year,
                    "status": fl.status,
                    "propositions": fl.propositions,
                    "citations": [_serialize_quoted_source(c) for c in fl.citations],
                    "niveau_certitude": fl.niveau_certitude.value,
                }
                for fl in forward_looking
            ]
            yield json.dumps({"type": "forward_looking", "data": fl_data}) + "\n"
            collected["forward_looking"] = fl_data

        # Jurisprudence
        _task = asyncio.create_task(asyncio.to_thread(analyzer.article_index.get_jurisprudence, article_ref))
        while not _task.done():
            try:
                await asyncio.wait_for(asyncio.shield(_task), timeout=30.0)
            except asyncio.TimeoutError:
                yield json.dumps({"type": "ping"}) + "\n"
        jurisprudence = _task.result()
        if jurisprudence:
            juris_data = [
                {
                    "juridiction": j.juridiction,
                    "date": j.date,
                    "reference": j.reference,
                    "titre": j.titre,
                    "faits": j.faits,
                    "solution": j.solution,
                    "portee": j.portee,
                }
                for j in jurisprudence
            ]
            yield json.dumps({"type": "jurisprudence", "data": juris_data}) + "\n"
            collected["jurisprudence"] = juris_data

        # Methodology references (rapports parlementaires + DILA sources)
        yield json.dumps({"type": "status", "data": "Gathering methodology references..."}) + "\n"
        _task = asyncio.create_task(asyncio.to_thread(analyzer._get_methodology_references, article_info))
        while not _task.done():
            try:
                await asyncio.wait_for(asyncio.shield(_task), timeout=30.0)
            except asyncio.TimeoutError:
                yield json.dumps({"type": "ping"}) + "\n"
        rapports, dila_sources = _task.result()
        collected["rapports_parlementaires"] = rapports
        collected["dila_sources"] = dila_sources
        if rapports:
            yield json.dumps({"type": "rapports_parlementaires", "data": rapports}) + "\n"
        if dila_sources:
            yield json.dumps({"type": "dila_sources", "data": dila_sources}) + "\n"

        # Parliamentary sources (AN + Sénat)
        _task = asyncio.create_task(asyncio.to_thread(analyzer._build_parliamentary_sources, sections))
        while not _task.done():
            try:
                await asyncio.wait_for(asyncio.shield(_task), timeout=30.0)
            except asyncio.TimeoutError:
                yield json.dumps({"type": "ping"}) + "\n"
        an_sources, senat_sources = _task.result()
        collected["sources_parlementaires_an"] = an_sources
        collected["sources_parlementaires_senat"] = senat_sources
        if an_sources or senat_sources:
            yield json.dumps({
                "type": "sources_parlementaires",
                "data": {"an": an_sources, "senat": senat_sources},
            }) + "\n"

        # Cross-law references detection
        yield json.dumps({"type": "status", "data": "Detecting cross-law references..."}) + "\n"
        cross_references = []
        try:
            cross_analyzer = CrossLawAnalyzer()
            sections_by_law: dict[str, dict] = {}
            debate_chunks_by_law: dict[str, list] = {}

            for section in sections:
                mod = section.modification
                law_key = f"{mod.law_type}_{mod.law_year}"
                sections_by_law[law_key] = {
                    "law_type": mod.law_type,
                    "law_year": mod.law_year,
                    "law_numero": mod.law_numero,
                }
                # Re-search to get chunks for cross-law detection
                if analyzer.search_service:
                    if mod.law_type in ("LF", "LFSS"):
                        ds = mod.date - timedelta(days=450)
                    else:
                        ds = mod.date - timedelta(days=180)
                    de = mod.date + timedelta(days=30)
                    try:
                        sr = analyzer.search_service.search_article_modification(
                            article_ref=article_info.reference,
                            article_keywords=article_info.keywords,
                            law_type=mod.law_type,
                            law_year=mod.law_year,
                            date_start=ds, date_end=de,
                            top_k=10, min_relevant=1,
                            article_in_law=mod.article_in_law,
                        )
                        debate_chunks_by_law[law_key] = sr.get("debates", [])
                    except Exception as search_err:
                        logger.debug(f"Cross-law search failed for {law_key}: {search_err}")

            if sections_by_law and debate_chunks_by_law:
                _task = asyncio.create_task(
                    asyncio.to_thread(
                        cross_analyzer.detect_cross_references,
                        sections_by_law, debate_chunks_by_law,
                    )
                )
                while not _task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(_task), timeout=30.0)
                    except asyncio.TimeoutError:
                        yield json.dumps({"type": "ping"}) + "\n"
                raw_refs = _task.result()
                cross_references = [
                    {
                        "source_law": f"{cr.source_law_type} {cr.source_law_year}",
                        "target_law": cr.target_law_ref,
                        "citation": cr.citation,
                    }
                    for cr in raw_refs
                ]
        except Exception as e:
            logger.warning(f"Cross-law detection failed unexpectedly: {e}")

        collected["cross_references"] = cross_references
        if cross_references:
            yield json.dumps({"type": "cross_references", "data": cross_references}) + "\n"

        # PLF-LF delta analysis
        yield json.dumps({"type": "status", "data": "Computing PLF-LF deltas..."}) + "\n"
        _task = asyncio.create_task(asyncio.to_thread(analyzer._compute_plf_lf_delta, article_info, sections))
        while not _task.done():
            try:
                await asyncio.wait_for(asyncio.shield(_task), timeout=30.0)
            except asyncio.TimeoutError:
                yield json.dumps({"type": "ping"}) + "\n"
        plf_lf_deltas = _task.result()
        collected["plf_lf_deltas"] = plf_lf_deltas
        if plf_lf_deltas:
            yield json.dumps({"type": "plf_lf_deltas", "data": plf_lf_deltas}) + "\n"

        # Compute metadata and invalidation data
        settings = get_settings()
        collected["metadata"] = {
            "total_sections": len(sections),
            "total_debates_searched": total_debates,
            "total_interventions_found": total_interventions,
            "generated_date": datetime.now(UTC).strftime("%Y-%m-%d"),
            "llm_model": settings.groq_model if settings.llm_provider == "groq" else settings.claude_model,
            "embedding_model": settings.local_embedding_model,
        }

        # Build invalidation hash from raw article index modifications
        index = get_article_index()
        raw_article = None
        for ref, data in index.get("articles", {}).items():
            if ref.lower() == article_ref.lower():
                raw_article = data
                break
        raw_modifications = raw_article.get("modifications", []) if raw_article else []
        mod_hash = compute_modifications_hash(raw_modifications)
        last_mod_date = raw_modifications[-1]["date"] if raw_modifications else ""

        collected["invalidation"] = {
            "modification_count": len(raw_modifications),
            "last_modification_date": last_mod_date,
            "modifications_hash": mod_hash,
        }

        # Compute quality metrics
        certitude_scores = {"élevé": 1.0, "moyen": 0.5, "faible": 0.0}
        section_scores = []
        total_sources = 0
        min_certitude = "élevé"
        for s in sections:
            score = certitude_scores.get(s.niveau_certitude.value, 0.5)
            source_count = len(s.sources_an) + len(s.sources_senat)
            section_scores.append(score)
            total_sources += source_count
            if certitude_scores.get(s.niveau_certitude.value, 0.5) < certitude_scores.get(min_certitude, 0.5):
                min_certitude = s.niveau_certitude.value

        overall_quality = sum(section_scores) / len(section_scores) if section_scores else 0.0

        # Persist complete analysis BEFORE the final yield
        # (code after the last yield may not execute if client disconnects)
        try:
            await asyncio.to_thread(
                save_cached_analysis,
                cgi_article=article_ref,
                analysis=collected,
                modifications_hash=mod_hash,
                modification_count=len(raw_modifications),
                last_modification_date=last_mod_date,
                quality_score=overall_quality,
                sources_count=total_sources,
                confidence_level=min_certitude,
            )
            collected["metadata"]["cached"] = True
        except Exception as cache_err:
            logger.error(f"Failed to persist analysis cache: {cache_err}")
            collected["metadata"]["cached"] = False

        # Done — final yield
        yield json.dumps({"type": "done", "data": collected["metadata"]}) + "\n"

    except Exception as e:
        logger.exception("Error in streaming analysis")
        yield json.dumps({"type": "error", "data": str(e)}) + "\n"


# ============================================================================
# Simple Async Analysis Endpoint (job-based, no streaming)
# ============================================================================

_analysis_jobs: dict[str, dict] = {}  # job_id → {status, error}


@cgi_router.post(
    "/{article_ref:path}/analyse",
    summary="Trigger article analysis",
    description=(
        "Launch a full article analysis in background. Returns immediately with a job_id. "
        "If a valid cached analysis already exists, returns it directly (from_cache=true). "
        "Poll GET /{article_ref}/analyse/status to track progress, "
        "then GET /{article_ref}/analysis to retrieve the result."
    ),
)
async def trigger_article_analysis(
    article_ref: str,
    question: str | None = Query(None, description="Custom question (default: standard legislative intent question)"),
    max_modifications: int | None = Query(None, description="Limit number of modifications to analyze"),
    force: bool = Query(False, description="Force re-analysis even if cache exists"),
):
    """Launch article analysis in background or return cached result immediately."""
    import uuid
    from src.analysis.article_analysis_service import analyze_article_for_api

    article_ref_normalized = normalize_article_ref(article_ref)

    # Return cache immediately if available (and not forced)
    if not force:
        cached = await asyncio.to_thread(get_cached_analysis, article_ref_normalized)
        if cached:
            cached["from_cache"] = True
            return JSONResponse(cached)

    # Check if a job is already running for this article
    existing_job = _analysis_jobs.get(article_ref_normalized)
    if existing_job and existing_job["status"] == "running":
        return JSONResponse(
            {"job_id": existing_job["job_id"], "status": "running", "article_ref": article_ref_normalized},
            status_code=202,
        )

    # Launch background job
    job_id = str(uuid.uuid4())
    _analysis_jobs[article_ref_normalized] = {"job_id": job_id, "status": "running", "error": None}

    async def _run():
        try:
            await asyncio.to_thread(
                analyze_article_for_api,
                article_ref_normalized,
                question,
                max_modifications,
                get_search_service(),
            )
            _analysis_jobs[article_ref_normalized]["status"] = "done"
        except Exception as e:
            logger.exception(f"Analysis job failed for {article_ref_normalized}")
            _analysis_jobs[article_ref_normalized]["status"] = "error"
            _analysis_jobs[article_ref_normalized]["error"] = str(e)
        finally:
            # Clean up completed jobs after a short delay to allow status polling
            await asyncio.sleep(300)  # Keep for 5 min so clients can poll the result
            _analysis_jobs.pop(article_ref_normalized, None)

    asyncio.create_task(_run())

    return JSONResponse(
        {"job_id": job_id, "status": "running", "article_ref": article_ref_normalized},
        status_code=202,
    )


@cgi_router.get(
    "/{article_ref:path}/analyse/status",
    summary="Check analysis job status",
    description="Check the status of a running analysis job. Returns 'running', 'done', or 'error'.",
)
async def get_analysis_status(article_ref: str):
    """Get the current status of an analysis job."""
    article_ref_normalized = normalize_article_ref(article_ref)
    job = _analysis_jobs.get(article_ref_normalized)

    if not job:
        # No active job — check if cache exists
        cached = await asyncio.to_thread(get_cached_analysis, article_ref_normalized)
        if cached:
            return JSONResponse({"status": "done", "article_ref": article_ref_normalized, "from_cache": True})
        return JSONResponse({"status": "not_started", "article_ref": article_ref_normalized})

    return JSONResponse({
        "job_id": job["job_id"],
        "status": job["status"],
        "article_ref": article_ref_normalized,
        "error": job.get("error"),
    })


# ============================================================================
# Cache Management Endpoints
# ============================================================================


@cgi_router.get(
    "/{article_ref:path}/analysis",
    summary="Get persisted analysis",
    description="Retrieve the persisted analysis for a CGI article. Returns 404 if no analysis is cached.",
)
async def get_article_analysis(article_ref: str):
    """Get persisted analysis for a CGI article (single JSON response)."""
    article_ref_normalized = normalize_article_ref(article_ref)

    cached = await asyncio.to_thread(get_cached_analysis, article_ref_normalized)
    if cached:
        result = {**cached, "from_cache": True}
        return JSONResponse(result)

    return JSONResponse(
        {"status": "not_found", "message": "No persisted analysis. Use /analysis/stream to generate."},
        status_code=404,
    )


@cgi_router.delete(
    "/{article_ref:path}/cache",
    summary="Clear cached analysis",
    description="Clear cached analysis for a specific CGI article.",
)
async def clear_cache(article_ref: str):
    """Clear cached analysis for a CGI article."""
    article_ref_normalized = normalize_article_ref(article_ref)
    with get_session() as session:
        try:
            count = (
                session.query(AnalysisCache)
                .filter(AnalysisCache.cgi_article == article_ref_normalized)
                .delete()
            )
            session.commit()
            return {"deleted": count, "article": article_ref}
        except Exception as e:
            session.rollback()
            logger.exception("Error clearing cache")
            raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Full Article Analysis Streaming Endpoint
# ============================================================================


def _build_cache_events(cached: dict) -> list[str]:
    """Build NDJSON event list from a cached analysis dict."""
    events: list[str] = []
    events.append(json.dumps({"type": "status", "data": "Serving from cache"}) + "\n")
    if cached.get("article_info"):
        events.append(json.dumps({"type": "article_info", "data": cached["article_info"]}) + "\n")
    for section in cached.get("sections", []):
        events.append(json.dumps({"type": "section", "data": section}) + "\n")
    for consol in cached.get("consolidation", []):
        events.append(json.dumps({"type": "consolidation", "data": consol}) + "\n")
    if cached.get("synthesis"):
        events.append(json.dumps({"type": "synthesis", "data": cached["synthesis"]}) + "\n")
    if cached.get("forward_looking"):
        events.append(json.dumps({"type": "forward_looking", "data": cached["forward_looking"]}) + "\n")
    if cached.get("jurisprudence"):
        events.append(json.dumps({"type": "jurisprudence", "data": cached["jurisprudence"]}) + "\n")
    if cached.get("rapports_parlementaires"):
        events.append(json.dumps({"type": "rapports_parlementaires", "data": cached["rapports_parlementaires"]}) + "\n")
    if cached.get("dila_sources"):
        events.append(json.dumps({"type": "dila_sources", "data": cached["dila_sources"]}) + "\n")
    if cached.get("sources_parlementaires_an") or cached.get("sources_parlementaires_senat"):
        events.append(json.dumps({
            "type": "sources_parlementaires",
            "data": {
                "an": cached.get("sources_parlementaires_an", []),
                "senat": cached.get("sources_parlementaires_senat", []),
            },
        }) + "\n")
    if cached.get("cross_references"):
        events.append(json.dumps({"type": "cross_references", "data": cached["cross_references"]}) + "\n")
    if cached.get("plf_lf_deltas"):
        events.append(json.dumps({"type": "plf_lf_deltas", "data": cached["plf_lf_deltas"]}) + "\n")
    meta = {**cached.get("metadata", {}), "from_cache": True}
    events.append(json.dumps({"type": "done", "data": meta}) + "\n")
    return events


@cgi_router.get(
    "/{article_ref:path}/analysis/stream",
    summary="Stream full article analysis",
    description="Stream comprehensive article analysis with progressive results. "
                "Each line is a JSON object (NDJSON format) with 'type' and 'data' fields. "
                "Types: status, article_info, section, consolidation, synthesis, "
                "forward_looking, jurisprudence, rapports_parlementaires, dila_sources, "
                "sources_parlementaires, cross_references, plf_lf_deltas, done, error.",
)
async def stream_article_analysis(
    request: Request,
    article_ref: str,
    question: str | None = Query(None, description="Custom question (default: standard legislative intent question)"),
    max_modifications: int | None = Query(None, ge=1, le=50, description="Max modifications to analyze"),
    force_refresh: bool = Query(False, description="Bypass cache and force re-analysis"),
):
    """Stream comprehensive article analysis for a CGI article."""
    # Normalize via centralized function first
    article_ref_normalized = normalize_article_ref(article_ref)

    # Also try to resolve via the analysis ArticleIndex (fuzzy matching)
    analysis_index = get_analysis_article_index()
    article_info = analysis_index.get_article(article_ref_normalized)
    if article_info:
        article_ref_normalized = article_info.reference
    elif not any(
        ref.lower() == article_ref_normalized.lower()
        for ref in get_article_index().get("articles", {})
    ):
        raise HTTPException(
            status_code=404,
            detail=f"CGI article '{article_ref}' not found",
        )

    # Build default question if none provided (needed for the analysis_key)
    if not question:
        question = f"Pourquoi le législateur a-t-il créé et modifié l'article {article_ref_normalized} ?"

    # Check if an identical analysis is already running — do this BEFORE the cache
    # check (which does an await) to close the race window where concurrent requests
    # all miss the key and each start their own analysis.
    analysis_key = f"{article_ref_normalized}::{question}::{max_modifications}"
    logger.info(f"Stream request for key={analysis_key!r}, running_keys={list(_running_analyses.keys())}")

    # Headers required for Chrome and all browsers to handle NDJSON streams correctly:
    # - no-cache/no-store: prevent HTTP caching of the stream
    # - X-Accel-Buffering: disable nginx proxy buffering
    _stream_headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "X-Accel-Buffering": "no",
    }

    # Subscribe to an ongoing analysis if one exists
    if analysis_key in _running_analyses:
        existing = _running_analyses[analysis_key]
        logger.info(f"Subscribing to ongoing analysis for {article_ref_normalized} (done={existing.done}, buffer_len={len(existing.buffer)})")
        return StreamingResponse(
            _stream_from_running(existing, request),
            media_type="application/x-ndjson",
            headers=_stream_headers,
        )

    # Check cache BEFORE reserving the key — cache hits return instantly without
    # touching the fan-out dict, so no subscriber race is possible.
    # Timeout prevents blocking when the thread pool is saturated by analysis work.
    if not force_refresh:
        try:
            cached = await asyncio.wait_for(
                asyncio.to_thread(get_cached_analysis, article_ref_normalized),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Cache check timed out for {article_ref_normalized}, falling through to fan-out path")
            cached = None
        if cached:
            logger.info(f"Cache hit for {article_ref_normalized}")
            cache_events = _build_cache_events(cached)

            async def stream_from_cache():
                for ev in cache_events:
                    yield ev

            return StreamingResponse(stream_from_cache(), media_type="application/x-ndjson", headers=_stream_headers)

    # No cache — reserve the key to prevent duplicate analyses, then launch task.
    # Between the cache check above and this setdefault, another request may have
    # reserved the key — setdefault handles that atomically.
    new_running = _RunningAnalysis()
    running = _running_analyses.setdefault(analysis_key, new_running)
    if running is not new_running:
        logger.info(f"Race condition resolved: subscribing to concurrent analysis for {article_ref_normalized}")
        return StreamingResponse(
            _stream_from_running(running, request),
            media_type="application/x-ndjson",
            headers=_stream_headers,
        )

    # No cache, key reserved — launch new analysis task
    logger.info(f"Launching new analysis for {article_ref_normalized}")
    asyncio.create_task(
        _run_analysis_task(article_ref_normalized, question, max_modifications, new_running, analysis_key)
    )

    return StreamingResponse(
        _stream_from_running(new_running, request),
        media_type="application/x-ndjson",
        headers=_stream_headers,
    )


# ============================================================================
# Catch-all Article Detail (MUST be last GET endpoint)
# ============================================================================


@cgi_router.get(
    "/{article_ref:path}",
    response_model=CGIArticleDetail,
    responses={404: {"model": ErrorResponse}},
    summary="Get CGI article details",
    description="Get detailed information about a CGI article including its modification history.",
)
async def get_cgi_article(article_ref: str):
    """Get detailed CGI article information."""
    try:
        index = get_article_index()
        articles_data = index.get("articles", {})

        # Normalize the reference for lookup
        article_ref_normalized = article_ref.replace("_", " ").replace("-", "-")

        if article_ref_normalized not in articles_data:
            # Try case-insensitive search
            for ref in articles_data:
                if ref.lower() == article_ref_normalized.lower():
                    article_ref_normalized = ref
                    break
            else:
                raise HTTPException(
                    status_code=404,
                    detail=f"CGI article '{article_ref}' not found",
                )

        data = articles_data[article_ref_normalized]

        modifications = [
            CGIModification(
                date=mod.get("date", ""),
                law_numero=mod.get("law_numero", ""),
                law_type=mod.get("law_type", ""),
                law_year=mod.get("law_year", 0),
                law_title=mod.get("law_title", ""),
                article_in_law=mod.get("article_in_law", ""),
                nature=mod.get("nature", "Modification"),
                legifrance_url=mod.get("legifrance_url", ""),
            )
            for mod in data.get("modifications", [])
        ]

        return CGIArticleDetail(
            reference=article_ref_normalized,
            titre=data.get("titre", ""),
            code=data.get("code", "CGI"),
            legifrance_url=data.get("legifrance_url", ""),
            bofip_url=data.get("bofip_url", ""),
            keywords=data.get("keywords", []),
            modifications=modifications,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error getting CGI article")
        raise HTTPException(status_code=500, detail=str(e))
