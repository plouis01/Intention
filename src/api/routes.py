"""API routes for health check and analysis law listing."""

import logging
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException

from src.api.schemas import (
    AvailableLawResponse,
    HealthResponse,
)
from src.db.models import Loi, get_session
from src.indexing.embeddings import EmbeddingService
from src.indexing.search import SearchService
from src.indexing.vector_stores import create_vector_store
from src.utils.config import get_settings

logger = logging.getLogger(__name__)

# Create routers
router = APIRouter()
analysis_router = APIRouter(prefix="/analysis", tags=["analysis"])

# Global services (initialized on startup)
_search_service: SearchService | None = None
_search_service_lock = threading.Lock()


def get_search_service() -> SearchService:
    """Get or initialize the search service (thread-safe)."""
    global _search_service

    if _search_service is not None:
        return _search_service

    with _search_service_lock:
        if _search_service is not None:
            return _search_service
        logger.info("Initializing search service...")
        settings = get_settings()

        # Build service in a local var so the global is only set once fully initialized
        svc = SearchService()

        # Load vector stores based on configuration
        vector_provider = settings.vector_store_provider
        embeddings_dir = Path("data/embeddings")

        debate_store_exists = (
            (embeddings_dir / "debate_store.npz").exists()
            or (embeddings_dir / "debate_store.json").exists()
        )
        if vector_provider == "memory" and debate_store_exists:
            logger.info("Loading vector stores from files...")
            svc.load_stores(embeddings_dir)
        elif vector_provider == "pgvector":
            # pgvector reads from chunk_embeddings with chunk_type filtering
            logger.info("Using pgvector store on chunk_embeddings...")
            pgvector_store = create_vector_store("pgvector")
            svc.debate_store = pgvector_store
            svc.amendment_store = pgvector_store
            logger.info(f"Connected to pgvector (chunks: {pgvector_store.count()})")
        elif vector_provider == "qdrant":
            logger.info("Using Qdrant vector store...")
            svc.debate_store = create_vector_store(
                "qdrant",
                collection_name=settings.qdrant_collection_debates,
            )
            svc.amendment_store = create_vector_store(
                "qdrant",
                collection_name=settings.qdrant_collection_amendments,
            )
            logger.info("Connected to Qdrant stores")
        else:
            logger.warning(
                f"Unknown or unconfigured vector store provider: {vector_provider!r}. "
                "No embeddings loaded. Set VECTOR_STORE_PROVIDER to 'pgvector', 'qdrant', "
                "or 'memory'."
            )

        # Publish only after fully initialized
        _search_service = svc

    return _search_service


# ============================================================================
# Health Check
# ============================================================================


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health_check():
    """Check API health and service status."""
    settings = get_settings()

    # Check database
    try:
        with get_session() as session:
            loi_count = session.query(Loi).count()
            db_status = f"ok ({loi_count} laws)"
    except Exception as e:
        db_status = f"error: {e}"

    # Check embeddings
    debate_count = 0
    amendment_count = 0
    try:
        service = get_search_service()
        debate_count = len(service.debate_store) if service.debate_store else 0
        amendment_count = len(service.amendment_store) if service.amendment_store else 0
    except Exception as e:
        logger.warning(f"Could not check embedding counts: {e}")

    return HealthResponse(
        status="ok",
        version="0.2.0",
        embedding_provider=settings.embedding_provider,
        llm_provider=settings.llm_provider,
        database_status=db_status,
        debate_chunks_count=debate_count,
        amendment_chunks_count=amendment_count,
    )


# ============================================================================
# Analysis Endpoints
# ============================================================================


@analysis_router.get(
    "/laws",
    response_model=list[AvailableLawResponse],
    summary="List available laws for analysis",
    description="Get a list of all finance laws available for analysis.",
)
async def list_available_laws():
    """List all available finance laws from the reference file."""
    try:
        from src.analysis.law_analyzer import list_available_laws as get_laws
        laws = get_laws()
        return [
            AvailableLawResponse(
                numero=law.get("numero", ""),
                type=law.get("type", ""),
                annee_budgetaire=law.get("annee_budgetaire", 0),
                titre=law.get("titre", ""),
                date_promulgation=law.get("date_promulgation", ""),
            )
            for law in laws
        ]
    except Exception as e:
        logger.exception("Error listing available laws")
        raise HTTPException(status_code=500, detail=str(e))
