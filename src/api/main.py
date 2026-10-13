"""FastAPI application for legislative intent search."""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from src.api.cgi_routes import cgi_router
from src.api.routes import analysis_router, router
from src.admin.routes import admin_router
from src.auth.routes import auth_router
from src.auth.trial import trial_router
from src.subscription.routes import subscription_router
from src.utils.config import get_settings
from src.utils.rate_limit import limiter

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

    # Default executor for asyncio.to_thread() (used by analysis path).
    # Sized to match --limit-concurrency 40 in Procfile.
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=40))

    settings = get_settings()
    logger.info(f"Embedding provider: {settings.embedding_provider}")
    logger.info(f"LLM provider: {settings.llm_provider}")

    # Initialize database
    from src.db.models import Base, get_engine, init_db
    init_db()
    # Create any new tables (additive-only, won't modify existing columns)
    engine = get_engine()
    Base.metadata.create_all(engine)

    # Add new columns to existing tables (create_all won't alter existing tables)
    from sqlalchemy import inspect as sa_inspect, text
    with engine.connect() as conn:
        inspector = sa_inspect(conn)
        notif_cols = [c["name"] for c in inspector.get_columns("notifications")]
        if "type" not in notif_cols:
            conn.execute(text("ALTER TABLE notifications ADD COLUMN type VARCHAR(50)"))
        if "metadata" not in notif_cols:
            conn.execute(text("ALTER TABLE notifications ADD COLUMN metadata TEXT"))
        contact_cols = [c["name"] for c in inspector.get_columns("contact_messages")]
        if "status" not in contact_cols:
            conn.execute(text("ALTER TABLE contact_messages ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'open'"))
        if "last_seen_at" not in contact_cols:
            conn.execute(text("ALTER TABLE contact_messages ADD COLUMN last_seen_at TIMESTAMP"))
        if inspector.has_table("conversation_messages"):
            conv_cols = [c["name"] for c in inspector.get_columns("conversation_messages")]
            if "file_path" not in conv_cols:
                conn.execute(text("ALTER TABLE conversation_messages ADD COLUMN file_path VARCHAR(500)"))
        # Trial activation on first open: add link_expires_at, make expires_at nullable
        trial_cols = {c["name"]: c for c in inspector.get_columns("trial_links")}
        if "link_expires_at" not in trial_cols:
            conn.execute(text("ALTER TABLE trial_links ADD COLUMN link_expires_at TIMESTAMP"))
            # Default link_expires_at to expires_at for existing rows (backward compat)
            conn.execute(text("UPDATE trial_links SET link_expires_at = expires_at WHERE link_expires_at IS NULL"))
            # Set pending status for links that were never redeemed
            conn.execute(text("UPDATE trial_links SET status = 'pending' WHERE redeemed_at IS NULL AND status = 'active'"))
        # Make expires_at nullable (separate check - column already exists but constraint may remain)
        if not trial_cols["expires_at"].get("nullable", True):
            conn.execute(text("ALTER TABLE trial_links ALTER COLUMN expires_at DROP NOT NULL"))
        conn.commit()

        # PR 3 - email_verified on users (default 1 for existing rows so legacy
        # accounts aren't locked out; new signups go through register_user
        # which sets 0 explicitly before emitting the verification email)
        user_cols = [c["name"] for c in inspector.get_columns("users")]
        if "email_verified" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0"))
            conn.execute(text("UPDATE users SET email_verified = 1"))
        conn.commit()

        if inspector.has_table("analysis_feedback"):
            fb_cols = [c["name"] for c in inspector.get_columns("analysis_feedback")]
            if "admin_reply" not in fb_cols:
                conn.execute(text("ALTER TABLE analysis_feedback ADD COLUMN admin_reply TEXT"))
            if "admin_reply_at" not in fb_cols:
                conn.execute(text("ALTER TABLE analysis_feedback ADD COLUMN admin_reply_at TIMESTAMP"))
            if "admin_reply_by" not in fb_cols:
                conn.execute(text("ALTER TABLE analysis_feedback ADD COLUMN admin_reply_by VARCHAR(36)"))
            conn.commit()

        # PR 5 - purge audit log rows older than 90 days
        try:
            conn.execute(text("DELETE FROM auth_events WHERE created_at < NOW() - INTERVAL '90 days'"))
            conn.commit()
        except Exception:
            # SQLite doesn't support INTERVAL syntax - fallback
            try:
                conn.execute(text("DELETE FROM auth_events WHERE created_at < datetime('now', '-90 days')"))
                conn.commit()
            except Exception as e:
                logger.warning(f"auth_events purge skipped: {e}")

    logger.info("Database initialized")

    # Preload vector stores so first request is fast
    try:
        from src.api.routes import get_search_service

        search_svc = get_search_service()
        logger.info("Vector stores preloaded")

        # Validate embedding dimensions - use the SearchService's own
        # EmbeddingService so the ONNX model is loaded on the instance
        # that will actually serve requests (not a throwaway instance)
        emb_service = search_svc.embedding_service
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

        # Preload reranker model - ONNX initialization holds the GIL for ~60-90s,
        # so it must happen at startup before accepting requests
        if search_svc.reranker is not None:
            search_svc.reranker._load_model()
            logger.info("Reranker model preloaded")
    except Exception as e:
        logger.warning(f"Could not preload vector stores: {e}")

    yield

    # Shutdown - release ML models and database connections
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

1. `GET /cgi` - Liste des articles CGI
2. `GET /cgi/{ref}` - Détail d'un article CGI
3. `GET /cgi/{ref}/analysis/stream` - Analyse en streaming (NDJSON)
4. `GET /cgi/{ref}/analysis` - Analyse persistée (JSON)
5. `DELETE /cgi/{ref}/cache` - Invalider le cache
6. `GET /analysis/laws` - Liste des lois de finances
7. `GET /health` - État du service
    """,
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Configure rate limiting (limiter defined in src.utils.rate_limit)
app.state.limiter = limiter


@app.exception_handler(429)
async def rate_limit_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=429,
        content={"detail": "Trop de requêtes. Réessayez dans quelques instants."},
    )


# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ensure uploads directory exists
os.makedirs(os.path.join(os.path.dirname(__file__), "..", "..", "data", "uploads"), exist_ok=True)

# Include routers
app.include_router(router)
app.include_router(analysis_router)
app.include_router(cgi_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(trial_router)
app.include_router(subscription_router)


# Entry point for running with uvicorn directly
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
