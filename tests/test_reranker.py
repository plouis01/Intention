"""Tests for the cross-encoder reranker module."""

from unittest.mock import MagicMock, patch

import pytest

from src.indexing.chunking import Chunk
from src.indexing.reranker import Reranker, get_reranker


def _make_chunk(content: str, source_id: str = "test") -> Chunk:
    return Chunk(
        content=content,
        metadata={},
        chunk_type="intervention",
        source_id=source_id,
        position=0,
    )


@pytest.fixture
def reranker_with_mock():
    """Create a Reranker with a mocked internal model."""
    reranker = Reranker(model_name="test-model")
    mock_model = MagicMock()
    reranker._model = mock_model
    return reranker, mock_model


class TestReranker:
    def test_rerank_reorders_chunks(self, reranker_with_mock):
        """Reranking should reorder chunks by cross-encoder scores."""
        reranker, mock_model = reranker_with_mock

        chunks = [
            (_make_chunk("Low relevance text"), 0.9),
            (_make_chunk("Medium relevance text"), 0.8),
            (_make_chunk("High relevance text"), 0.7),
        ]

        # Cross-encoder scores reverse the bi-encoder ordering
        mock_model.predict.return_value = [0.1, 0.5, 0.9]

        result = reranker.rerank("test query", chunks)

        assert len(result) == 3
        # Highest cross-encoder score first
        assert result[0][0].content == "High relevance text"
        assert result[0][1] == pytest.approx(0.9)
        assert result[1][0].content == "Medium relevance text"
        assert result[1][1] == pytest.approx(0.5)
        assert result[2][0].content == "Low relevance text"
        assert result[2][1] == pytest.approx(0.1)

    def test_rerank_respects_top_k(self, reranker_with_mock):
        """Reranking with top_k should truncate results."""
        reranker, mock_model = reranker_with_mock

        chunks = [
            (_make_chunk(f"Chunk {i}"), float(i) / 10)
            for i in range(5)
        ]
        mock_model.predict.return_value = [0.5, 0.1, 0.9, 0.3, 0.7]

        result = reranker.rerank("test query", chunks, top_k=2)

        assert len(result) == 2
        assert result[0][1] == pytest.approx(0.9)
        assert result[1][1] == pytest.approx(0.7)

    def test_rerank_empty_input(self, reranker_with_mock):
        """Reranking empty list should return empty list without calling model."""
        reranker, mock_model = reranker_with_mock

        result = reranker.rerank("test query", [])

        assert result == []
        mock_model.predict.assert_not_called()

    def test_rerank_truncates_long_content(self, reranker_with_mock):
        """Long chunk content should be truncated before passing to cross-encoder."""
        reranker, mock_model = reranker_with_mock
        long_content = "A" * 3000
        chunks = [(_make_chunk(long_content), 0.5)]
        mock_model.predict.return_value = [0.8]

        reranker.rerank("query", chunks)

        # Verify the pair passed to predict has truncated content
        call_args = mock_model.predict.call_args[0][0]
        assert len(call_args[0][1]) == 1500

    def test_lazy_model_loading(self):
        """Model should not be loaded until rerank() is called."""
        reranker = Reranker(model_name="test-model")
        assert reranker._model is None

        # Patch the import inside _load_model
        mock_ce_class = MagicMock()
        mock_ce_instance = MagicMock()
        mock_ce_class.return_value = mock_ce_instance
        mock_ce_instance.predict.return_value = [0.8]

        with patch.dict(
            "sys.modules",
            {"sentence_transformers": MagicMock(CrossEncoder=mock_ce_class)},
        ):
            chunks = [(_make_chunk("text"), 0.5)]
            reranker.rerank("query", chunks)

        assert reranker._model is not None


class TestGetReranker:
    def test_returns_none_when_disabled(self):
        """get_reranker() should return None when reranker_enabled=False."""
        get_reranker.cache_clear()
        with patch("src.utils.config.get_settings") as mock_settings:
            mock_settings.return_value.reranker_enabled = False
            result = get_reranker()
            assert result is None
        get_reranker.cache_clear()

    def test_returns_reranker_when_enabled(self):
        """get_reranker() should return a Reranker when enabled."""
        get_reranker.cache_clear()
        with patch("src.utils.config.get_settings") as mock_settings:
            mock_settings.return_value.reranker_enabled = True
            mock_settings.return_value.reranker_model = "test-model"
            result = get_reranker()
            assert isinstance(result, Reranker)
            assert result.model_name == "test-model"
        get_reranker.cache_clear()
