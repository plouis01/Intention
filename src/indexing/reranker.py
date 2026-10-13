"""Cross-encoder reranker for rescoring search results."""

import logging
from functools import lru_cache

from src.indexing.chunking import Chunk

logger = logging.getLogger(__name__)

# Maximum characters of chunk content to pass to cross-encoder.
# Cross-encoders have a 512-token limit; ~1500 chars is conservative for French.
_MAX_CONTENT_CHARS = 1500


class Reranker:
    """Cross-encoder reranker for rescoring search results.

    Uses a cross-encoder model that jointly encodes (query, document) pairs
    to produce more accurate relevance scores than bi-encoder similarity.
    The model is lazy-loaded on first call to rerank().
    """

    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None

    def _load_model(self):
        """Lazy-load the CrossEncoder model."""
        if self._model is not None:
            return
        from sentence_transformers import CrossEncoder

        logger.info(f"Loading cross-encoder model: {self.model_name}")
        self._model = CrossEncoder(self.model_name)
        logger.info("Cross-encoder model loaded")

    def rerank(
        self,
        query: str,
        chunks: list[tuple[Chunk, float]],
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> list[tuple[Chunk, float]]:
        """Rerank chunks using cross-encoder scoring.

        Args:
            query: The search query (natural language).
            chunks: List of (chunk, bi_encoder_score) from initial retrieval.
            top_k: Number of results to return (None = return all, re-sorted).
            min_score: Minimum cross-encoder score to keep. Chunks below this
                threshold are discarded as irrelevant. For ms-marco models,
                scores are logits where negative ≈ irrelevant.

        Returns:
            Reranked list of (chunk, cross_encoder_score).
        """
        if not chunks:
            return []

        self._load_model()

        # Build (query, document) pairs, truncating long content
        pairs = [
            (query, chunk.content[:_MAX_CONTENT_CHARS])
            for chunk, _ in chunks
        ]

        # Score all pairs in one batch
        scores = self._model.predict(pairs)

        # Pair chunks with cross-encoder scores and sort descending
        scored = sorted(
            zip([c for c, _ in chunks], scores),
            key=lambda x: float(x[1]),
            reverse=True,
        )

        results = [(chunk, float(score)) for chunk, score in scored]

        # Filter by minimum score threshold
        if min_score is not None:
            results = [(c, s) for c, s in results if s >= min_score]

        if top_k is not None:
            results = results[:top_k]

        return results


@lru_cache
def get_reranker() -> Reranker | None:
    """Get cached reranker instance, or None if disabled.

    Reads reranker_enabled and reranker_model from settings.
    """
    from src.utils.config import get_settings

    settings = get_settings()
    if not settings.reranker_enabled:
        return None

    return Reranker(model_name=settings.reranker_model)
