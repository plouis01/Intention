"""Tests for ONNX backend configuration - Story 2.1.

Validates that SentenceTransformer and CrossEncoder use ONNX backend.
"""

class TestONNXEmbeddingBackend:
    """Story 2.1: Verify SentenceTransformer uses ONNX backend."""

    def test_embedding_service_uses_onnx(self):
        """EmbeddingService local model should use ONNX backend."""
        from src.indexing.embeddings import EmbeddingService

        service = EmbeddingService()
        # Force local provider
        service.provider = "local"
        service.settings.embedding_provider = "local"

        model = service.local_model
        # When backend="onnx", the Transformer module's auto_model is an ORT model
        transformer = model[0]  # First module is Transformer
        auto_model_type = type(transformer.auto_model).__name__
        assert "ORT" in auto_model_type or "onnx" in auto_model_type.lower(), (
            f"Expected ONNX/ORT model, got: {auto_model_type}"
        )

    def test_embedding_output_valid(self):
        """ONNX embedding should produce valid float vectors."""
        from src.indexing.embeddings import EmbeddingService

        service = EmbeddingService()
        service.provider = "local"

        embedding = service._generate_local_embedding("Test de texte juridique français")
        assert isinstance(embedding, list)
        assert len(embedding) > 0
        assert all(isinstance(x, float) for x in embedding)

    def test_embedding_dimension_matches_config(self):
        """ONNX embedding dimension should match expected model dimension."""
        from src.indexing.embeddings import EmbeddingService

        service = EmbeddingService()
        service.provider = "local"

        embedding = service._generate_local_embedding("Article 150-0 B ter du CGI")
        # paraphrase-multilingual-MiniLM-L12-v2 produces 384-dim embeddings
        assert len(embedding) == 384


class TestONNXRerankerBackend:
    """Story 2.1: Verify CrossEncoder uses ONNX backend."""

    def test_reranker_uses_onnx(self):
        """Reranker model should use ONNX backend."""
        from src.indexing.reranker import Reranker

        reranker = Reranker(model_name="cross-encoder/ms-marco-MiniLM-L-6-v2")
        reranker._load_model()

        model = reranker._model
        # CrossEncoder stores the ORT model in model.model
        auto_model_type = type(model.model).__name__
        assert "ORT" in auto_model_type or "onnx" in auto_model_type.lower(), (
            f"Expected ONNX/ORT model in reranker, got: {auto_model_type}"
        )

    def test_reranker_scores_valid(self):
        """ONNX reranker should produce valid scores."""
        from src.indexing.chunking import Chunk
        from src.indexing.reranker import Reranker

        reranker = Reranker(model_name="cross-encoder/ms-marco-MiniLM-L-6-v2")

        chunks = [
            (Chunk(content="Article 150-0 B ter relatif aux plus-values", chunk_type="intervention", metadata={}, source_id="1", position=0), 0.8),
            (Chunk(content="La météo est belle aujourd'hui", chunk_type="intervention", metadata={}, source_id="2", position=0), 0.5),
        ]

        results = reranker.rerank("article 150-0 B ter plus-values", chunks)
        assert len(results) == 2
        # Relevant chunk should score higher
        assert results[0][1] > results[1][1]
        assert results[0][0].content.startswith("Article 150")
