"""FastAPI application for legislative intent search."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.cgi_routes import cgi_router
from src.api.routes import analysis_router, router
from src.utils.config import get_settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    logger.info("Starting up...")
    settings = get_settings()
    logger.info(f"Embedding provider: {settings.embedding_provider}")
    logger.info(f"LLM provider: {settings.llm_provider}")

    # Initialize database
    from src.db.models import Base, get_engine, init_db
    init_db()
    # Create any new tables (additive-only, won't modify existing columns)
    engine = get_engine()
    Base.metadata.create_all(engine)
    logger.info("Database initialized")

    # Preload vector stores so first request is fast
    try:
        from src.api.routes import get_search_service

        search_svc = get_search_service()
        logger.info("Vector stores preloaded")

        # Validate embedding dimensions match the vector store
        from src.indexing.embeddings import EmbeddingService

        emb_service = EmbeddingService()
        test_embedding = emb_service.generate_embedding("test de dimension", use_cache=False)
        emb_dim = len(test_embedding)
        store = search_svc.debate_store
        if store and hasattr(store, "embedding_dim"):
            store_dim = store.embedding_dim
            if emb_dim != store_dim:
                raise RuntimeError(
                    f"Embedding dimension mismatch: model produces {emb_dim}d vectors "
                    f"but vector store expects {store_dim}d. "
                    f"Regenerate embeddings or change EMBEDDING_PROVIDER/LOCAL_EMBEDDING_MODEL."
                )
            logger.info(f"Embedding dimension validated: {emb_dim}d")
    except Exception as e:
        logger.warning(f"Could not preload vector stores: {e}")

    yield

    # Shutdown — release ML models and database connections
    logger.info("Shutting down...")

    # Cleanup ML models to release memory
    try:
        from src.api.routes import _search_service
        if _search_service is not None and hasattr(_search_service, 'embedding_service'):
            emb_svc = _search_service.embedding_service
            if hasattr(emb_svc, '_local_model') and emb_svc._local_model is not None:
                del emb_svc._local_model
                emb_svc._local_model = None
                logger.info("Embedding model released")

        from src.indexing.reranker import get_reranker
        reranker = get_reranker()
        if reranker is not None and reranker._model is not None:
            del reranker._model
            reranker._model = None
            logger.info("Reranker model released")
    except Exception as e:
        logger.warning(f"Error during ML model cleanup: {e}")

    # Dispose database engine to release connections
    try:
        from src.db.models import _engine
        if _engine is not None:
            _engine.dispose()
            logger.info("Database engine disposed")
    except Exception as e:
        logger.warning(f"Error during DB cleanup: {e}")

    logger.info("Shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Intention du Législateur API",
    description="""
API pour l'analyse de l'intention du législateur dans les lois de finances françaises (2012-2025).

## Fonctionnalités

- **Articles CGI** : Consultez les articles du Code Général des Impôts modifiés par les lois de finances
- **Analyse complète** : Analysez l'intention du législateur avec streaming NDJSON progressif
- **Cache intelligent** : Analyses persistées avec invalidation automatique
- **Lois de référence** : Liste des 35 lois de finances (2012-2025)

## Endpoints principaux

1. `GET /cgi` — Liste des articles CGI
2. `GET /cgi/{ref}` — Détail d'un article CGI
3. `GET /cgi/{ref}/analysis/stream` — Analyse en streaming (NDJSON)
4. `GET /cgi/{ref}/analysis` — Analyse persistée (JSON)
5. `DELETE /cgi/{ref}/cache` — Invalider le cache
6. `GET /analysis/laws` — Liste des lois de finances
7. `GET /health` — État du service
    """,
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(router)
app.include_router(analysis_router)
app.include_router(cgi_router)


# Entry point for running with uvicorn directly
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
