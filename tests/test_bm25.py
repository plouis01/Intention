"""Tests for BM25 sparse retrieval module."""

import json
import tempfile
from pathlib import Path

from src.indexing.bm25 import BM25Index, tokenize
from src.indexing.chunking import Chunk
from src.indexing.search import reciprocal_rank_fusion

# -- Helpers --

def _make_chunk(content: str, source_id: str = "1", position: int = 0) -> Chunk:
    return Chunk(
        content=content,
        metadata={},
        chunk_type="intervention",
        source_id=source_id,
        position=position,
    )


# -- Tokenization --

def test_tokenize_basic():
    tokens = tokenize("Le rapporteur général présente l'article 150-0 B ter du CGI.")
    assert "rapporteur" in tokens
    assert "général" in tokens
    assert "150-0" in tokens
    assert "ter" in tokens
    assert "cgi" in tokens
    # Stopwords removed
    assert "le" not in tokens
    assert "du" not in tokens


def test_tokenize_preserves_hyphens():
    """Article references like 150-0 should be kept as single tokens."""
    tokens = tokenize("article 150-0 B ter et 199 terdecies-0 A")
    assert "150-0" in tokens
    assert "terdecies-0" in tokens


def test_tokenize_removes_french_stopwords():
    tokens = tokenize("le la les de du des un une et en au aux")
    assert len(tokens) == 0


def test_tokenize_html_entities():
    tokens = tokenize("l&#39;article &amp; les &eacute;lections")
    assert "article" in tokens


def test_tokenize_short_tokens_removed():
    """Single-character tokens should be filtered out."""
    tokens = tokenize("a b c de le la mot")
    assert "mot" in tokens
    assert "de" not in tokens  # stopword
    assert len([t for t in tokens if len(t) < 2]) == 0


# -- BM25Index build & search --

def test_bm25_build_and_search():
    chunks = [
        _make_chunk(
            "Le rapporteur général présente le dispositif de l'article 150-0 B ter "
            "relatif à l'apport-cession et au report d'imposition.",
            source_id="1",
        ),
        _make_chunk(
            "Discussion sur le budget de l'éducation nationale et les crédits "
            "du ministère pour l'année 2024.",
            source_id="2",
        ),
        _make_chunk(
            "L'amendement vise à modifier l'article 150-0 B ter du code général "
            "des impôts pour renforcer les conditions de réinvestissement.",
            source_id="3",
        ),
    ]

    index = BM25Index()
    index.build(chunks)

    results = index.search("article 150-0 B ter apport-cession", top_k=3)
    assert len(results) >= 2

    # The two chunks about 150-0 B ter should rank higher
    result_ids = [c.source_id for c, _ in results]
    assert "1" in result_ids[:2]
    assert "3" in result_ids[:2]


def test_bm25_empty_index():
    index = BM25Index()
    index.build([])
    results = index.search("test query")
    assert results == []


def test_bm25_search_no_build():
    """Search on unbuilt index returns empty."""
    index = BM25Index()
    assert index.search("test") == []


def test_bm25_empty_query():
    chunks = [_make_chunk("Some content here")]
    index = BM25Index()
    index.build(chunks)
    # Query with only stopwords
    results = index.search("le la les de du")
    assert results == []


# -- Persistence (save/load) --

def test_bm25_save_load():
    chunks = [
        _make_chunk("L'article 150-0 B ter du CGI concerne l'apport-cession.", "1"),
        _make_chunk("Le budget de l'État pour l'exercice 2024.", "2"),
    ]

    index = BM25Index()
    index.build(chunks)

    # Search before save
    results_before = index.search("article 150-0 B ter", top_k=2)

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "bm25_tokens.json"
        index.save(path)

        # Verify file exists and is valid JSON
        assert path.exists()
        with open(path) as f:
            data = json.load(f)
        assert len(data) == 2

        # Load into new index
        new_index = BM25Index()
        new_index.load(path, chunks)

        results_after = new_index.search("article 150-0 B ter", top_k=2)

    # Same results after round-trip
    assert len(results_before) == len(results_after)
    for (c1, s1), (c2, s2) in zip(results_before, results_after):
        assert c1.source_id == c2.source_id
        assert abs(s1 - s2) < 1e-6


def test_bm25_load_mismatched_count_rebuilds():
    """If token count doesn't match chunk count, rebuild from scratch."""
    chunks = [
        _make_chunk("Content A", "1"),
        _make_chunk("Content B", "2"),
    ]

    index = BM25Index()
    index.build(chunks)

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "bm25_tokens.json"
        index.save(path)

        # Load with different number of chunks
        new_chunks = [_make_chunk("Content A", "1")]
        new_index = BM25Index()
        new_index.load(path, new_chunks)

        # Should have rebuilt with 1 chunk
        assert len(new_index) == 1


# -- Reciprocal Rank Fusion --

def test_rrf_fusion():
    c1 = _make_chunk("chunk 1", "1")
    c2 = _make_chunk("chunk 2", "2")
    c3 = _make_chunk("chunk 3", "3")
    c4 = _make_chunk("chunk 4", "4")

    dense = [(c1, 0.9), (c2, 0.8), (c3, 0.7)]
    sparse = [(c2, 5.0), (c4, 4.0), (c1, 3.0)]

    fused = reciprocal_rank_fusion(dense, sparse, k=60, top_k=4)

    # c1 and c2 appear in both lists, so they should have higher RRF scores
    fused_ids = [c.source_id for c, _ in fused]
    # c2 is rank 1 in sparse and rank 2 in dense — should be top
    # c1 is rank 1 in dense and rank 3 in sparse — should be second
    assert fused_ids[0] in ("1", "2")
    assert fused_ids[1] in ("1", "2")
    assert len(fused) == 4


def test_rrf_fusion_no_overlap():
    """RRF with disjoint result sets."""
    c1 = _make_chunk("chunk 1", "1")
    c2 = _make_chunk("chunk 2", "2")

    dense = [(c1, 0.9)]
    sparse = [(c2, 5.0)]

    fused = reciprocal_rank_fusion(dense, sparse, k=60, top_k=2)
    assert len(fused) == 2


def test_rrf_fusion_empty_sparse():
    c1 = _make_chunk("chunk 1", "1")
    dense = [(c1, 0.9)]
    sparse = []

    fused = reciprocal_rank_fusion(dense, sparse, k=60, top_k=5)
    assert len(fused) == 1
    assert fused[0][0].source_id == "1"


def test_rrf_fusion_respects_top_k():
    chunks = [_make_chunk(f"chunk {i}", str(i)) for i in range(20)]
    dense = [(c, 1.0 - i * 0.01) for i, c in enumerate(chunks)]
    sparse = [(c, 20.0 - i) for i, c in enumerate(chunks)]

    fused = reciprocal_rank_fusion(dense, sparse, top_k=5)
    assert len(fused) == 5
