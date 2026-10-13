"""BM25 sparse retrieval index over chunks."""

import json
import logging
import re
from pathlib import Path

from src.indexing.chunking import Chunk

logger = logging.getLogger(__name__)

# Minimal French stopwords — no NLTK dependency
FRENCH_STOPWORDS = frozenset({
    "le", "la", "les", "de", "du", "des", "un", "une",
    "et", "en", "au", "aux", "ce", "ces", "se", "sa", "son", "ses",
    "il", "elle", "ils", "elles", "on", "nous", "vous",
    "je", "tu", "me", "te", "ne", "pas", "que", "qui", "est",
    "sur", "par", "pour", "dans", "avec", "plus", "ou", "où",
    "mais", "si", "ni", "car", "donc", "été", "sont", "ont",
    "cette", "tout", "tous", "très", "aussi", "bien",
})

# Tokenization regex: split on non-alphanumeric except hyphens.
# Hyphens are preserved inside tokens (e.g. "150-0") but stripped at boundaries.
_TOKEN_RE = re.compile(r"[a-zà-ÿ0-9]+(?:-[a-zà-ÿ0-9]+)*", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """French-aware tokenization for BM25.

    - Lowercases and unescapes HTML entities
    - Splits on non-alphanumeric (preserving hyphens for article refs like "150-0")
    - Removes French stopwords
    - Keeps tokens with len >= 2
    """
    import html as _html

    text = _html.unescape(text).lower()
    tokens = _TOKEN_RE.findall(text)
    return [t for t in tokens if len(t) >= 2 and t not in FRENCH_STOPWORDS]


class BM25Index:
    """BM25 sparse retrieval index over chunks."""

    def __init__(self, chunks: list[Chunk] | None = None):
        self.chunks: list[Chunk] = chunks or []
        self._bm25 = None  # rank_bm25.BM25Okapi instance
        self._tokenized: list[list[str]] = []

    def build(self, chunks: list[Chunk]) -> None:
        """Build BM25 index from chunks. Tokenizes French text."""
        self.chunks = list(chunks)
        if not self.chunks:
            self._tokenized = []
            self._bm25 = None
            return

        from rank_bm25 import BM25Okapi

        self._tokenized = [tokenize(c.content) for c in self.chunks]
        self._bm25 = BM25Okapi(self._tokenized)
        logger.info(f"Built BM25 index with {len(self.chunks)} documents")

    def search(self, query: str, top_k: int = 20) -> list[tuple[Chunk, float]]:
        """Search BM25 index. Returns (chunk, bm25_score) sorted desc."""
        if self._bm25 is None or not self.chunks:
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        scores = self._bm25.get_scores(query_tokens)

        # Get top_k indices sorted by score descending
        if len(scores) <= top_k:
            top_indices = sorted(range(len(scores)), key=lambda i: -scores[i])
        else:
            top_indices = sorted(
                range(len(scores)), key=lambda i: -scores[i]
            )[:top_k]

        return [
            (self.chunks[i], float(scores[i]))
            for i in top_indices
            if scores[i] > 0
        ]

    def save(self, path: Path) -> None:
        """Save tokenized corpus to disk as JSON.

        Chunks are already persisted in VectorStore metadata —
        only the tokenized corpus is saved here for fast BM25 rebuild.
        """
        path = Path(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._tokenized, f, ensure_ascii=False)
        logger.info(f"Saved BM25 tokens to {path} ({len(self._tokenized)} docs)")

    def load(self, path: Path, chunks: list[Chunk]) -> None:
        """Load pre-tokenized corpus, rebuild BM25 index.

        Args:
            path: Path to saved tokenized corpus JSON
            chunks: Chunk list in same order as VectorStore (same indexing)
        """
        from rank_bm25 import BM25Okapi

        path = Path(path)
        with open(path, encoding="utf-8") as f:
            self._tokenized = json.load(f)

        self.chunks = list(chunks)

        if len(self._tokenized) != len(self.chunks):
            logger.warning(
                f"BM25 token count ({len(self._tokenized)}) != chunk count ({len(self.chunks)}). "
                "Rebuilding from scratch."
            )
            self.build(self.chunks)
            return

        self._bm25 = BM25Okapi(self._tokenized)
        logger.info(f"Loaded BM25 index with {len(self.chunks)} documents from {path}")

    def __len__(self) -> int:
        return len(self.chunks)
