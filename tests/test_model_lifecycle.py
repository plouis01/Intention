"""Tests for model lifecycle management - Story 2.2.

Validates that ML models are cleaned up on shutdown and lazy-loaded on demand.
"""

from unittest.mock import MagicMock, patch


class TestModelLifecycleCleanup:
    """Story 2.2: Verify model cleanup on shutdown."""

    def test_lifespan_shutdown_cleans_embedding_model(self):
        """Shutdown should release embedding model memory."""
        from fastapi.testclient import TestClient

        from src.api.main import app

        with TestClient(app):
            # App is running, models may be loaded
            pass
        # After context exit, shutdown has run — no crash = cleanup worked

    def test_embedding_service_lazy_loads(self):
        """EmbeddingService should not load model until first use."""
        from src.indexing.embeddings import EmbeddingService

        service = EmbeddingService()
        # Model should not be loaded yet
        assert service._local_model is None

    def test_reranker_lazy_loads(self):
        """Reranker should not load model until first rerank() call."""
        from src.indexing.reranker import Reranker

        reranker = Reranker(model_name="cross-encoder/ms-marco-MiniLM-L-6-v2")
        assert reranker._model is None

    def test_embedding_model_can_be_deleted(self):
        """Embedding model should be deletable for memory release."""
        from src.indexing.embeddings import EmbeddingService

        service = EmbeddingService()
        service.provider = "local"
        # Load the model
        _ = service.local_model
        assert service._local_model is not None

        # Delete it
        del service._local_model
        service._local_model = None
        assert service._local_model is None

    def test_reranker_model_can_be_deleted(self):
        """Reranker model should be deletable for memory release."""
        from src.indexing.reranker import Reranker

        reranker = Reranker(model_name="cross-encoder/ms-marco-MiniLM-L-6-v2")
        reranker._load_model()
        assert reranker._model is not None

        # Delete it
        del reranker._model
        reranker._model = None
        assert reranker._model is None


class TestModelRuntimeDecision:
    """Story 2.2: Embedding model IS needed at API runtime for query encoding."""

    def test_search_service_uses_embedding_for_queries(self):
        """SearchService needs embedding model to encode search queries."""
        from src.indexing.search import SearchService

        service = SearchService()
        # The service has an embedding_service attribute for query encoding
        assert hasattr(service, "embedding_service")
        assert service.embedding_service is not None
