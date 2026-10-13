"""API routes for CGI article endpoints with caching and streaming.

Railway production deployment:
  1. Backup DB via Railway dashboard
  2. Run scripts/migrate_to_pgvector_safe.sql (~2h30 for 1.6M chunks)
  3. Monitor: SELECT * FROM pg_stat_progress_create_index;
  4. Set env: VECTOR_STORE_PROVIDER=pgvector
  5. Redeploy - config.py handles postgres:// → postgresql:// conversion
  6. See docs/pgvector-next-steps.md for details
"""

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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

from src.auth.dependencies import require_active_access, require_active_access_stream
from src.db.models import AnalysisCache, User, get_session
from src.utils.config import get_settings

logger = logging.getLogger(__name__)

# Create router
cgi_router = APIRouter(prefix="/cgi", tags=["cgi"])

# Global services
_article_index: dict | None = None
_article_index_mtime: float = 0.0


def get_article_index() -> dict:
    """Load and cache the modifications_cgi_enriched.json index, auto-reloading on file change."""
    global _article_index, _article_index_mtime

    if _article_index is not None and _article_index_mtime == 0.0:
        # Set externally (e.g., by tests) - don't override
        return _article_index

    index_path = Path("data/modifications_cgi_enriched.json")

    if index_path.exists():
        current_mtime = index_path.stat().st_mtime
        if _article_index is None or current_mtime != _article_index_mtime:
            with open(index_path, encoding="utf-8") as f:
                _article_index = json.load(f)
            _article_index_mtime = current_mtime
            logger.info(f"Loaded {len(_article_index.get('articles', {}))} CGI articles from index")
    elif _article_index is None:
        logger.warning("modifications_cgi_enriched.json not found")
        _article_index = {"articles": {}}

    return _article_index


def normalize_article_ref(article_ref: str) -> str:
    """Normalize article reference to canonical form from the index.

    Applies underscore→space conversion and case-insensitive lookup.
    Returns the canonical reference from modifications_cgi_enriched.json
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
    """Check if cached analysis is stale vs current modifications_cgi_enriched.json.

    Returns:
        (is_stale, current_hash) - True if stale or no cache exists.
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
                logger.info(f"Cache MISS for {cgi_article}: no entry in DB")
                return None

            # Check expiration (compare as naive UTC - DB column has no timezone)
            if cache.expires_at:
                expires = cache.expires_at.replace(tzinfo=None)
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                if expires < now:
                    logger.info(
                        f"Cache EXPIRED for {cgi_article}: "
                        f"expires_at={cache.expires_at}, now={now}"
                    )
                    session.delete(cache)
                    session.commit()
                    return None

            # Check invalidation inline (avoid second DB query)
            current_hash = _compute_current_hash(cgi_article)
            if current_hash and cache.modifications_hash != current_hash:
                logger.info(
                    f"Cache STALE for {cgi_article}: "
                    f"cached_hash={cache.modifications_hash[:16] if cache.modifications_hash else 'None'}... "
                    f"current_hash={current_hash[:16]}... "
                    f"cached_mod_count={cache.modification_count} "
                    f"cached_last_mod={cache.last_modification_date}"
                )
                session.delete(cache)
                session.commit()
                return None

            logger.info(f"Cache HIT for {cgi_article} (hash={current_hash[:16]}...)")
            return cache.analysis_json

    except Exception as e:
        logger.warning(f"Cache ERROR for {cgi_article}: {e}")
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
    logger.info(
        f"Cache SAVE for {cgi_article}: hash={modifications_hash[:16]}... "
        f"mod_count={modification_count} last_mod={last_modification_date}"
    )
    with get_session() as session:
        try:
            settings = get_settings()

            existing = (
                session.query(AnalysisCache)
                .filter(AnalysisCache.cgi_article == cgi_article)
                .first()
            )

            # Store as naive UTC (DB column has no timezone info)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
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
                    # Another request inserted concurrently - update instead
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
def list_cgi_articles(
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
def list_cached_refs() -> JSONResponse:
    """Return the set of CGI article refs with a valid cached analysis."""
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
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
    d = {
        "citation": source.citation,
        "orateur": source.orateur,
        "fonction": source.fonction,
        "date": source.date,
        "chambre": source.chambre,
        "is_curated": source.is_curated,
    }
    if getattr(source, "passage_cle", ""):
        d["passage_cle"] = source.passage_cle
    if getattr(source, "extrait_id", ""):
        d["extrait_id"] = source.extrait_id
    return d


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
        "doctrine_convergence": getattr(section, 'doctrine_convergence', ''),
        "doctrine_references": getattr(section, 'doctrine_references', []),
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

# Bounds the number of concurrent DIFFERENT analyses. Identical analyses are
# already deduplicated via _running_analyses fan-out. A request beyond the limit
# queues at this semaphore instead of saturating the heavy DB pool / executor.
#
# Tunable via env var MAX_CONCURRENT_ANALYSES (default 2). Bump on Railway dashboard
# without redeploy if metrics show headroom (CPU, RAM, no Anthropic 429s).
_MAX_CONCURRENT_ANALYSES = max(1, int(os.getenv("MAX_CONCURRENT_ANALYSES", "2")))
_ANALYSIS_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_ANALYSES)
logger.info(f"Analysis semaphore configured: max {_MAX_CONCURRENT_ANALYSES} concurrent analyses")


def _broadcast_event(running: _RunningAnalysis, event: str) -> None:
    """Append an event to the buffer and fan-out to live subscribers."""
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


async def _run_analysis_task(
    article_ref: str,
    question: str,
    max_modifications: int | None,
    running: _RunningAnalysis,
    analysis_key: str,
) -> None:
    """Background task: runs the analysis and fan-outs events. Lifecycle is independent of HTTP connections."""
    try:
        if _ANALYSIS_SEMAPHORE.locked():
            _broadcast_event(
                running,
                json.dumps({"type": "queued", "data": "Analyse en file d'attente..."}) + "\n",
            )
        async with _ANALYSIS_SEMAPHORE:
            async for event in _run_analysis(article_ref, question, max_modifications):
                _broadcast_event(running, event)
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
    # If already done: replay full buffer and return - None sentinel already sent, no queue needed
    if running.done:
        buffer_snapshot = list(running.buffer)
        logger.debug(f"[stream] done=True at entry, replaying {len(buffer_snapshot)} buffered events")
        for event in buffer_snapshot:
            yield event
            await asyncio.sleep(0)
        return

    # Analysis still running: snapshot buffer + subscribe atomically
    # (asyncio single-threaded - no await between snapshot and append)
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    buffered = list(running.buffer)
    running.subscribers.append(q)

    # Re-check done AFTER subscribing: the task's finally block may have run between
    # our initial done-check above and this append - if so, the None sentinel was
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
            "cross_references": [],
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

        # Era cutoff: an identifier can be (re-)created mid-history (e.g. CGI 973
        # was abrogated and re-created as IFI by LF 2018 — n°2017-1837). Pre-Création
        # entries refer to a DIFFERENT article reusing the same number; feeding
        # them to the LLM produces structural false positives (5/8 sections of
        # 973 were analysing the OLD article before this guard). The cutoff
        # is no-op when no "Création" event exists in the history (articles
        # with continuous lineage, e.g. 39 C since the 1960s) or when the
        # "Création" is already the earliest entry (e.g. 150-0 B ter, 2012).
        _creation_dates = [
            m.date for m in modifications
            if m.nature.value == "Création"
        ]
        if _creation_dates:
            era_cutoff = max(_creation_dates)
            _pre = [m for m in modifications if m.date < era_cutoff]
            if _pre:
                logger.warning(
                    "[era_cutoff] %s: dropping %d pre-Création modifications "
                    "(cutoff=%s). These refer to a prior article re-using the "
                    "same identifier.", article_ref, len(_pre), era_cutoff,
                )
                modifications = [m for m in modifications if m.date >= era_cutoff]

        if max_modifications:
            modifications = modifications[:max_modifications]

        # Separate modifications into analyzable (LF/LFR/LFSS) vs skippable (DECRET etc.)
        _NO_DEBATE_TYPES = {"DECRET", "ORDONNANCE", "ARRETE", "LOI_ORGANIQUE"}
        _TECHNICAL_NATURES = {"Codification", "Transfert"}
        analyzable_mods = []
        skipped_mods = []
        for m in modifications:
            if m.law_type in _NO_DEBATE_TYPES or m.nature.value in _TECHNICAL_NATURES or m.law_type == "INCONNU":
                skipped_mods.append(m)
            else:
                analyzable_mods.append(m)

        # Sort by DATE (chronological), then group by law_numero
        sorted_mods = sorted(analyzable_mods, key=lambda m: m.date)
        grouped_mods: list[list] = []
        for _key, group in groupby(sorted_mods, key=lambda m: m.law_numero):
            grouped_mods.append(list(group))

        # Merge groups that are temporally close (±60 days), same article_in_law,
        # AND same law_type. LF + LFR are distinct parliamentary texts and must NOT
        # be fused - only true duplicates (e.g., two LFSS entries for the same law).
        merged_groups: list[list] = []
        for group in grouped_mods:
            if merged_groups:
                prev = merged_groups[-1]
                prev_date = prev[0].date
                curr_date = group[0].date
                if (abs((curr_date - prev_date).days) <= 60
                        and prev[0].article_in_law == group[0].article_in_law
                        and prev[0].law_type == group[0].law_type):
                    merged_groups[-1] = prev + group
                    continue
            merged_groups.append(group)
        grouped_mods = merged_groups

        # Build chronological timeline: analyzable groups + skipped mods interleaved
        # so the user sees a complete modification history (DECRET appear as lightweight sections)
        timeline: list[tuple[str, list]] = []
        for group in grouped_mods:
            timeline.append(("analyze", group))
        for m in skipped_mods:
            timeline.append(("skip", [m]))
        timeline.sort(key=lambda x: x[1][0].date)

        total_sections = len(timeline)
        total_analyzable = sum(1 for t, _ in timeline if t == "analyze")

        # Yield article info
        article_info_data = {
            "reference": article_info.reference,
            "titre": article_info.titre,
            "code": article_info.code,
            "question": question,
            "total_modifications": len(modifications),
            "total_sections": total_sections,
            "modifications": [
                {"date": m.date.isoformat(), "law_ref": m.law_ref, "nature": m.nature.value}
                for m in modifications
            ],
        }
        yield json.dumps({"type": "article_info", "data": article_info_data}) + "\n"
        collected["article_info"] = article_info_data

        # Process timeline
        sections = []
        total_debates = 0
        total_interventions = 0
        token_idx = 0
        used_citation_keys: set[tuple[str, str, str]] = set()
        seen_amendment_numeros: set[str] = set()
        _roman_re = re.compile(r'^[IVX]+-\s*', re.IGNORECASE)
        debate_chunks_by_law: dict[str, list] = {}
        analyze_idx = 0

        for i, (action, mod_group) in enumerate(timeline):
            primary_mod = mod_group[0]

            # Lightweight section for DECRET/ORDONNANCE - no LLM call, just metadata
            if action == "skip":
                objet_text = (primary_mod.objet or "").strip()[:500]
                skip_data = {
                    "index": i,
                    "modification": {
                        "date": primary_mod.date.isoformat(),
                        "law_numero": primary_mod.law_numero,
                        "law_type": primary_mod.law_type,
                        "law_year": primary_mod.law_year,
                        "article_in_law": primary_mod.article_in_law,
                        "nature": primary_mod.nature.value,
                        "law_ref": primary_mod.law_ref,
                    },
                    "contexte": "",
                    "intention": f"{primary_mod.law_type} - pas de débat parlementaire."
                                + (f" {objet_text}" if objet_text else ""),
                    "sources_an": [],
                    "sources_senat": [],
                    "evolution_debats": "",
                    "points_vigilance": [],
                    "niveau_certitude": "faible",
                    "certitude_justification": f"{primary_mod.law_type} sans débat parlementaire",
                    "debates_analyzed": 0,
                    "amendments_analyzed": 0,
                    "operational_params": None,
                    "skipped": True,
                }
                yield json.dumps({"type": "section", "data": skip_data}) + "\n"
                collected["sections"].append(skip_data)
                continue

            analyze_idx += 1
            yield json.dumps({
                "type": "status",
                "data": f"Analyzing {primary_mod.short_ref} ({analyze_idx}/{total_analyzable})...",
            }) + "\n"

            try:
                # Build all modification dates for cross-contamination prevention
                all_mod_dates = [(m.date, m.law_type) for m in modifications]
                _task = asyncio.create_task(
                    asyncio.to_thread(analyzer._analyze_modification, article_info, primary_mod, all_mod_dates)
                )
                while not _task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(_task), timeout=30.0)
                    except asyncio.TimeoutError:
                        yield json.dumps({"type": "ping"}) + "\n"
                section, debates_count, interventions_count, raw_debate_chunks = _task.result()

                # Collect raw debate chunks for cross-law detection (avoids re-searching)
                law_key = f"{primary_mod.law_type}_{primary_mod.law_year}"
                debate_chunks_by_law[law_key] = raw_debate_chunks

                # Cross-section dedup: remove citations already used in previous sections
                # Include date so that different interventions by the same speaker
                # on different dates (= different law modifications) are kept.
                def _citation_key(src):
                    return (src.orateur.lower(), src.citation[:80].lower(), src.date or "")

                section.sources_an = [
                    s for s in section.sources_an if _citation_key(s) not in used_citation_keys
                ]
                section.sources_senat = [
                    s for s in section.sources_senat if _citation_key(s) not in used_citation_keys
                ]
                for s in section.sources_an + section.sources_senat:
                    used_citation_keys.add(_citation_key(s))

                # Cross-period filter: remove citations whose date is closer to another modification
                if all_mod_dates:
                    def _citation_belongs_to_current(citation_date_str):
                        try:
                            cit_date = date.fromisoformat(citation_date_str[:10]) if citation_date_str else None
                        except (ValueError, TypeError):
                            return True
                        if not cit_date:
                            return True
                        current_dist = abs((primary_mod.date - cit_date).days)
                        for mod_dt, _ in all_mod_dates:
                            if mod_dt == primary_mod.date:
                                continue
                            if abs((mod_dt - cit_date).days) < current_dist:
                                return False
                        return True

                    section.sources_an = [s for s in section.sources_an if _citation_belongs_to_current(s.date)]
                    section.sources_senat = [s for s in section.sources_senat if _citation_belongs_to_current(s.date)]

                # Recalculate certitude after cross-section/cross-period filtering
                from src.analysis.models import CertitudeLevel
                actual_src = len(section.sources_an) + len(section.sources_senat)
                actual_ch = (1 if section.sources_an else 0) + (1 if section.sources_senat else 0)
                if not section.sources_an and not section.sources_senat:
                    section.niveau_certitude = CertitudeLevel.FAIBLE
                elif section.niveau_certitude == CertitudeLevel.ELEVE:
                    if actual_src < 2 or actual_ch < 2:
                        section.niveau_certitude = CertitudeLevel.MOYEN
                # Downgrade MOYEN → FAIBLE if only 1 source from 1 chamber
                if section.niveau_certitude == CertitudeLevel.MOYEN:
                    if actual_src <= 1 and actual_ch <= 1:
                        section.niveau_certitude = CertitudeLevel.FAIBLE

                # Merge all articles from the group into the section
                if len(mod_group) > 1:
                    all_articles = ", ".join(dict.fromkeys(m.article_in_law for m in mod_group))
                    section.modification = dataclasses.replace(
                        primary_mod, article_in_law=all_articles
                    )

                # P10: Clear params for minor modifications
                objet = (section.modification.objet or "").lower()
                _substantive = {"ajout", "suppression", "remplacement", "création", "modification du"}
                is_minor = "modification mineure" in objet
                is_forme = objet == "modification mineure (mise en forme)"
                if (is_minor or is_forme) and not any(v in objet for v in _substantive):
                    section.operational_params = None

                # Cross-section dedup: amendements_rejetes_detailles
                unique_amends = []
                for amend in section.amendements_rejetes_detailles:
                    num = amend.get('numero', '')
                    num_norm = _roman_re.sub('', num).strip() if num else ''
                    if num_norm and num_norm not in seen_amendment_numeros:
                        seen_amendment_numeros.add(num_norm)
                        unique_amends.append(amend)
                section.amendements_rejetes_detailles = unique_amends

                # Cross-section dedup: points_vigilance by amendment number
                unique_pv = []
                for pv in section.points_vigilance:
                    m = re.search(r'N°\s*([IVX]*-?\s*\d+)', pv)
                    if m:
                        num = _roman_re.sub('', m.group(1)).strip()
                        if num in seen_amendment_numeros:
                            continue
                        seen_amendment_numeros.add(num)
                    unique_pv.append(pv)
                section.points_vigilance = unique_pv

                section_data = _serialize_section(section, i)
                # Attach per-section token usage
                from src.analysis.models import TokenTracker
                section_calls = analyzer.token_tracker.calls[token_idx:]
                token_idx = len(analyzer.token_tracker.calls)
                section_data["token_usage"] = TokenTracker.summarize_calls(section_calls)
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

        # --- Post-section steps: run all in parallel ---
        yield json.dumps({"type": "status", "data": "Finalizing analysis..."}) + "\n"

        # Build cross-law data structures (no network, just dict building)
        cross_analyzer = CrossLawAnalyzer()
        settings = get_settings()
        sections_by_law: dict[str, dict] = {}
        for section in sections:
            mod = section.modification
            law_key = f"{mod.law_type}_{mod.law_year}"
            sections_by_law[law_key] = {
                "law_type": mod.law_type,
                "law_year": mod.law_year,
                "law_numero": mod.law_numero,
            }

        # Launch all independent post-section tasks in parallel
        task_consolidation = asyncio.create_task(
            asyncio.to_thread(analyzer._detect_consolidation_periods, article_info, modifications)
        )
        task_synthesis = asyncio.create_task(
            asyncio.to_thread(analyzer._build_synthesis, sections)
        )
        task_parl_sources = asyncio.create_task(
            asyncio.to_thread(analyzer._build_parliamentary_sources, sections)
        )
        task_cross_law = asyncio.create_task(
            asyncio.to_thread(
                cross_analyzer.detect_all,
                sections_by_law, debate_chunks_by_law,
                article_ref,
                settings.cross_ref_min_score,
                settings.cross_ref_enable_llm,
            )
        ) if sections_by_law and debate_chunks_by_law else None

        # Wait for all with keepalive pings
        all_tasks = [t for t in [
            task_consolidation, task_synthesis,
            task_parl_sources, task_cross_law,
        ] if t is not None]
        pending = set(all_tasks)
        while pending:
            done, pending = await asyncio.wait(pending, timeout=30.0, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                yield json.dumps({"type": "ping"}) + "\n"

        # Yield results in consistent order

        # Consolidation
        try:
            consolidation_periods = task_consolidation.result()
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
        try:
            synthese = task_synthesis.result()
        except Exception as synthesis_err:
            logger.exception("Error building synthesis")
            yield json.dumps({"type": "error", "data": f"Synthesis failed: {synthesis_err}"}) + "\n"
            synthese = {"evolution": []}
        synthesis_data = {
            "evolution": [
                {"periode": r.periode, **r.parameters}
                for r in synthese.get("evolution", [])
            ],
        }
        yield json.dumps({"type": "synthesis", "data": synthesis_data}) + "\n"
        collected["synthesis"] = synthesis_data

        # Parliamentary sources
        an_sources, senat_sources = task_parl_sources.result()
        collected["sources_parlementaires_an"] = an_sources
        collected["sources_parlementaires_senat"] = senat_sources
        if an_sources or senat_sources:
            yield json.dumps({
                "type": "sources_parlementaires",
                "data": {"an": an_sources, "senat": senat_sources},
            }) + "\n"

        # Cross-law references
        cross_references = []
        try:
            raw_refs = task_cross_law.result() if task_cross_law else []
            cross_references = [
                {
                    "source_law": f"{cr.source_law_type} {cr.source_law_year}",
                    "target_law": cr.target_law_ref,
                    "citation": cr.citation,
                    "ref_type": cr.ref_type.value,
                    "confidence": round(cr.confidence, 3),
                }
                for cr in raw_refs
            ]
        except Exception as e:
            logger.warning(f"Cross-law detection failed unexpectedly: {e}")
        collected["cross_references"] = cross_references
        if cross_references:
            yield json.dumps({"type": "cross_references", "data": cross_references}) + "\n"

        # Jurisprudence from DB (verified, not LLM-generated)
        jurisprudence_data = []
        try:
            jurisprudence_data = await asyncio.to_thread(
                analyzer._fetch_jurisprudence_from_db, article_ref
            )
        except Exception as e:
            logger.warning(f"Jurisprudence fetch failed: {e}")
        collected["jurisprudence"] = jurisprudence_data
        if jurisprudence_data:
            yield json.dumps({"type": "jurisprudence", "data": jurisprudence_data}) + "\n"

        # Compute metadata and invalidation data
        settings = get_settings()
        collected["metadata"] = {
            "total_sections": len(sections),
            "total_debates_searched": total_debates,
            "total_interventions_found": total_interventions,
            "generated_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
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

        # Emit token usage summary
        token_summary = analyzer.token_tracker.to_dict()
        yield json.dumps({"type": "token_usage", "data": token_summary}) + "\n"
        collected["metadata"]["token_usage"] = token_summary
        collected["metadata"]["estimated_cost_usd"] = analyzer.token_tracker.estimated_cost_usd()

        # Done - final yield
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
    _user: User = Depends(require_active_access),
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
        # No active job - check if cache exists
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
def clear_cache(article_ref: str):
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
    if cached.get("jurisprudence"):
        events.append(json.dumps({"type": "jurisprudence", "data": cached["jurisprudence"]}) + "\n")
    meta = {**cached.get("metadata", {}), "from_cache": True}
    events.append(json.dumps({"type": "done", "data": meta}) + "\n")
    return events


@cgi_router.get(
    "/{article_ref:path}/analysis/stream",
    summary="Stream full article analysis",
    description="Stream comprehensive article analysis with progressive results. "
                "Each line is a JSON object (NDJSON format) with 'type' and 'data' fields. "
                "Types: status, article_info, section, consolidation, synthesis, "
                "sources_parlementaires, cross_references, done, error.",
)
async def stream_article_analysis(
    request: Request,
    article_ref: str,
    question: str | None = Query(None, description="Custom question (default: standard legislative intent question)"),
    max_modifications: int | None = Query(None, ge=1, le=50, description="Max modifications to analyze"),
    force_refresh: bool = Query(False, description="Bypass cache and force re-analysis"),
    _user: User = Depends(require_active_access_stream),
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

    # Check if an identical analysis is already running - do this BEFORE the cache
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

    # Check cache BEFORE reserving the key - cache hits return instantly without
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
            # Re-check if another request started an analysis during the timeout
            if analysis_key in _running_analyses:
                existing = _running_analyses[analysis_key]
                logger.info(f"Subscribing to analysis started during cache timeout for {article_ref_normalized}")
                return StreamingResponse(
                    _stream_from_running(existing, request),
                    media_type="application/x-ndjson",
                    headers=_stream_headers,
                )
        if cached:
            logger.info(f"Cache hit for {article_ref_normalized}")
            cache_events = _build_cache_events(cached)

            async def stream_from_cache():
                for ev in cache_events:
                    yield ev

            return StreamingResponse(stream_from_cache(), media_type="application/x-ndjson", headers=_stream_headers)

    # No cache - reserve the key to prevent duplicate analyses, then launch task.
    # Between the cache check above and this setdefault, another request may have
    # reserved the key - setdefault handles that atomically.
    new_running = _RunningAnalysis()
    running = _running_analyses.setdefault(analysis_key, new_running)
    if running is not new_running:
        logger.info(f"Race condition resolved: subscribing to concurrent analysis for {article_ref_normalized}")
        return StreamingResponse(
            _stream_from_running(running, request),
            media_type="application/x-ndjson",
            headers=_stream_headers,
        )

    # No cache, key reserved - launch new analysis task
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
def get_cgi_article(article_ref: str):
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
            etat_actuel=data.get("etat_actuel", ""),
            keywords=data.get("keywords", []),
            modifications=modifications,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error getting CGI article")
        raise HTTPException(status_code=500, detail=str(e))
