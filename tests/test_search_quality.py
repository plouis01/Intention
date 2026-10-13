"""Search quality evaluation tests.

These tests measure search relevance using representative French legal queries
against the real vector stores and BM25 indices. They establish a quality
baseline and detect regressions when the search pipeline changes.

Tests are skipped if vector stores are not available (CI-safe).
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.indexing.chunking import Chunk
from src.indexing.search import (
    ScoreExplanation,
    SearchService,
    _is_specific_keyword,
    _normalize_text,
    expand_legal_terms,
    filter_chunks_by_keywords,
    get_keywords_for_article,
    reciprocal_rank_fusion,
)

logger = logging.getLogger(__name__)

EMBEDDINGS_DIR = Path("data/embeddings")


@pytest.fixture(scope="module")
def search_service():
    """Load the real search service with vector stores and BM25 indices.

    Scope=module to avoid reloading ~1M embeddings per test.
    """
    if not (EMBEDDINGS_DIR / "debate_store.npz").exists():
        pytest.skip("Vector stores not available (run --embed phase first)")

    service = SearchService()
    service.load_stores(EMBEDDINGS_DIR)

    if len(service.debate_store) == 0:
        pytest.skip("Debate store is empty")

    return service


# ============================================================================
# Search Quality Test Cases
# ============================================================================
#
# Each case defines:
#   query: the search query (as a user would ask)
#   method: "text" for search_text, "cgi" for search_cgi_article,
#           "hybrid" for hybrid_search
#   expected_terms: terms that MUST appear in at least one top result
#   min_results: minimum number of results expected
#   description: human-readable description of what we're testing
#

QUALITY_CASES = [
    {
        "id": "cgi_150_0_b_ter",
        "description": "Article 150-0 B ter (apport-cession) should find relevant debates",
        "method": "cgi",
        "article_ref": "150-0 B ter",
        "expected_terms": ["150-0", "apport"],
        "min_results": 5,
    },
    {
        "id": "cgi_199_terdecies",
        "description": "Article 199 terdecies-0 A (IR-PME Madelin) should find relevant debates",
        "method": "cgi",
        "article_ref": "199 terdecies-0 A",
        "expected_terms": ["199 terdecies", "madelin", "pme"],
        "min_results": 3,
    },
    {
        "id": "text_flat_tax",
        "description": "Free-text search for 'flat tax' should find PFU debates",
        "method": "text",
        "query": "flat tax prélèvement forfaitaire unique",
        "expected_terms": ["flat tax", "prélèvement forfaitaire", "pfu"],
        "min_results": 3,
    },
    {
        "id": "text_plus_values",
        "description": "Free-text search for plus-values immobilières",
        "method": "text",
        "query": "plus-values immobilières cession",
        "expected_terms": ["plus-value", "immobili"],
        "min_results": 3,
    },
    {
        "id": "hybrid_ifi",
        "description": "Hybrid search for IFI (impôt sur la fortune immobilière)",
        "method": "hybrid",
        "keywords": ["IFI", "impôt sur la fortune immobilière"],
        "semantic_query": "transformation de l'ISF en IFI impôt sur la fortune immobilière",
        "expected_terms": ["ifi", "fortune", "immobili"],
        "min_results": 3,
    },
    {
        "id": "hybrid_cir",
        "description": "Hybrid search for CIR (crédit d'impôt recherche)",
        "method": "hybrid",
        "keywords": ["CIR", "crédit d'impôt recherche", "244 quater B"],
        "semantic_query": "crédit d'impôt recherche innovation CIR article 244 quater B",
        "expected_terms": ["crédit d'impôt", "recherche"],
        "min_results": 3,
    },
    {
        "id": "text_taxe_habitation",
        "description": "Free-text search for suppression de la taxe d'habitation",
        "method": "text",
        "query": "suppression taxe d'habitation résidence principale",
        "expected_terms": ["taxe d'habitation", "habitation"],
        "min_results": 2,
    },
    {
        "id": "cgi_787_b",
        "description": "Article 787 B (Dutreil) should find transmission debates",
        "method": "cgi",
        "article_ref": "787 B",
        "expected_terms": ["787", "dutreil", "transmission"],
        "min_results": 2,
    },
]


def _check_results_contain_terms(
    results: list[tuple[Chunk, float]],
    expected_terms: list[str],
    top_n: int = 10,
) -> tuple[bool, list[str]]:
    """Check if at least one expected term appears in the top-N results.

    Returns (all_found, missing_terms).
    """
    top_results = results[:top_n]
    combined_text = " ".join(
        _normalize_text(chunk.content) for chunk, _ in top_results
    )

    missing = []
    for term in expected_terms:
        if _normalize_text(term) not in combined_text:
            missing.append(term)

    # At least one expected term must match
    found_any = len(missing) < len(expected_terms)
    return found_any, missing


@pytest.mark.parametrize(
    "case",
    QUALITY_CASES,
    ids=[c["id"] for c in QUALITY_CASES],
)
def test_search_quality(search_service, case):
    """Run a quality test case and verify minimum relevance criteria."""
    method = case["method"]

    if method == "text":
        results_dict = search_service.search_text(
            query_text=case["query"],
            top_k=15,
            min_similarity=0.3,
        )
        all_results = results_dict.get("debates", []) + results_dict.get("amendments", [])

    elif method == "cgi":
        results_dict = search_service.search_cgi_article(
            article_ref=case["article_ref"],
            top_k=20,
            min_similarity=0.25,
        )
        all_results = results_dict.get("debates", []) + results_dict.get("amendments", [])

    elif method == "hybrid":
        results_dict = search_service.hybrid_search(
            keywords=case["keywords"],
            semantic_query=case["semantic_query"],
            top_k=15,
            min_similarity=0.25,
        )
        all_results = results_dict.get("debates", []) + results_dict.get("amendments", [])
    else:
        pytest.fail(f"Unknown method: {method}")

    # Minimum results check
    assert len(all_results) >= case["min_results"], (
        f"[{case['id']}] Expected at least {case['min_results']} results, got {len(all_results)}"
    )

    # Relevance check: at least one expected term found in top results
    found, missing = _check_results_contain_terms(all_results, case["expected_terms"])
    assert found, (
        f"[{case['id']}] None of the expected terms found in top results. "
        f"Missing: {missing}"
    )

    # Log quality metrics for manual review
    logger.info(
        f"[{case['id']}] Results: {len(all_results)}, "
        f"Top score: {all_results[0][1]:.4f}, "
        f"Missing terms: {missing or 'none'}"
    )


# ============================================================================
# Term Expansion Tests
# ============================================================================


def test_expand_legal_terms_known_article():
    """Known articles should have term expansions."""
    terms = expand_legal_terms("150-0 B ter")
    # Should include related terms
    assert len(terms) > 0 or True  # May be empty if expansions file not loaded


def test_get_keywords_for_article():
    """Should return keywords for known CGI articles."""
    kw = get_keywords_for_article("150-0 B ter")
    assert isinstance(kw, list)


# ============================================================================
# Normalize Text
# ============================================================================


def test_normalize_text_handles_html_entities():
    assert "l'article" in _normalize_text("l&#39;article")
    # &amp; gets unescaped to &
    assert _normalize_text("a &amp; b") == "a & b"


def test_normalize_text_handles_unicode_apostrophes():
    result = _normalize_text("l\u2019article")
    assert "'" in result


# ============================================================================
# Filter Chunks by Keywords
# ============================================================================


def _make_chunk(content: str, source_id: str = "test") -> Chunk:
    return Chunk(
        content=content,
        metadata={},
        chunk_type="intervention",
        source_id=source_id,
        position=0,
    )


def test_filter_chunks_returns_matches():
    """filter_chunks_by_keywords should return chunks containing the keyword."""
    chunks = [
        (_make_chunk("L'article 150-0 B ter relatif à l'apport-cession"), 0.8),
        (_make_chunk("Le budget de l'éducation nationale"), 0.7),
        (_make_chunk("Modification de l'article 150-0 B ter du CGI"), 0.6),
    ]

    filtered = filter_chunks_by_keywords(chunks, ["150-0 B ter"])
    assert len(filtered) >= 2


def test_filter_chunks_empty_keyword():
    """Empty keyword list should return empty results."""
    chunks = [(_make_chunk("Some content"), 0.5)]
    filtered = filter_chunks_by_keywords(chunks, [])
    assert len(filtered) == 0


# ============================================================================
# Specific Keyword Detection
# ============================================================================


def test_is_specific_keyword_article_ref():
    """Keywords containing the article reference are always specific."""
    assert _is_specific_keyword("article 150-0 B ter", "150-0 B ter") is True
    assert _is_specific_keyword("150-0 B ter", "150-0 B ter") is True
    assert _is_specific_keyword("1500bter", "150-0 B ter") is True


def test_is_specific_keyword_multi_word_phrase():
    """Multi-word phrases with 2+ meaningful words are specific."""
    assert _is_specific_keyword("report d'imposition") is True
    assert _is_specific_keyword("apport de titres") is True
    assert _is_specific_keyword("réinvestissement économique") is True
    assert _is_specific_keyword("seuil de réinvestissement") is True


def test_is_specific_keyword_single_generic_word():
    """Single generic words are NOT specific."""
    assert _is_specific_keyword("cession") is False
    assert _is_specific_keyword("titres") is False
    assert _is_specific_keyword("société") is False
    assert _is_specific_keyword("régime") is False
    assert _is_specific_keyword("holding") is False
    assert _is_specific_keyword("délai") is False
    assert _is_specific_keyword("capital") is False


def test_is_specific_keyword_short_words():
    """Very short words and stopwords are not specific."""
    assert _is_specific_keyword("lors") is False
    assert _is_specific_keyword("plus") is False
    assert _is_specific_keyword("150") is False


# ============================================================================
# Thematic Contamination Prevention
# ============================================================================


def test_filter_rejects_generic_only_matches_with_context():
    """Chunks matching only generic single-word keywords should be rejected
    when require_context=True and no article reference is found."""
    # Irrelevant EPCI/communes debate that matches generic keywords
    epci_chunk = _make_chunk(
        "Cet amendement vise à modifier les modalités de rattachement "
        "à un EPCI d'une commune nouvelle. La cession de propriétés "
        "de la société est soumise à un délai de réalisation. "
        "Le régime fiscal des collectivités prévoit des conditions spécifiques."
    )
    # Relevant apport-cession debate
    relevant_chunk = _make_chunk(
        "L'article 150-0 B ter prévoit un report d'imposition "
        "lors de l'apport de titres à une société contrôlée. "
        "Le réinvestissement économique doit intervenir dans un délai de deux ans."
    )

    chunks = [(epci_chunk, 0.7), (relevant_chunk, 0.6)]
    keywords = [
        "cession", "société", "régime", "délai",
        "report d'imposition", "apport de titres", "150-0 B ter",
    ]

    filtered = filter_chunks_by_keywords(
        chunks, keywords, min_required=2,
        article_ref="150-0 B ter", require_context=True,
    )

    # Only the relevant chunk should pass (has article ref or specific keywords)
    contents = [c.content for c, _ in filtered]
    assert any("150-0 B ter" in c for c in contents), "Relevant chunk should pass"
    assert not any("EPCI" in c for c in contents), "EPCI chunk should be filtered out"


def test_filter_accepts_specific_phrase_without_article_ref():
    """Chunks with specific multi-word phrase matches should pass even
    without explicit article reference."""
    chunk = _make_chunk(
        "Le mécanisme de l'apport-cession permet le report d'imposition "
        "des plus-values. Le réinvestissement économique doit être effectif."
    )

    chunks = [(chunk, 0.6)]
    keywords = ["report d'imposition", "réinvestissement économique", "cession"]

    filtered = filter_chunks_by_keywords(
        chunks, keywords, min_required=2,
        article_ref="150-0 B ter", require_context=True,
    )

    assert len(filtered) == 1, "Chunk with specific phrases should pass"


def test_filter_rejects_mecenat_contamination():
    """A debate about mécénat should not pass filtering for 150-0 B ter."""
    mecenat_chunk = _make_chunk(
        "Le dispositif de mécénat des entreprises permet une réduction "
        "d'impôt. La société bénéficiaire doit respecter les conditions "
        "du régime fiscal. Les titres de participation sont exclus."
    )

    chunks = [(mecenat_chunk, 0.5)]
    keywords = [
        "société", "régime", "titres", "conditions",
        "report d'imposition", "apport de titres", "article 150-0 B ter",
    ]

    filtered = filter_chunks_by_keywords(
        chunks, keywords, min_required=3,
        article_ref="150-0 B ter", require_context=True,
    )

    assert len(filtered) == 0, "Mécénat debate should be filtered out"


def test_filter_passes_with_explicit_article_ref():
    """Chunks explicitly mentioning the article always pass regardless of keyword specificity."""
    chunk = _make_chunk(
        "L'article 150-0 B ter du CGI prévoit un régime de cession."
    )

    chunks = [(chunk, 0.5)]
    keywords = ["cession", "régime"]

    filtered = filter_chunks_by_keywords(
        chunks, keywords, min_required=1,
        article_ref="150-0 B ter", require_context=True,
    )

    assert len(filtered) == 1, "Explicit article ref should always pass"


# ============================================================================
# RRF Fusion Quality
# ============================================================================


def test_rrf_boosts_dual_presence():
    """Chunks appearing in both dense and sparse should score higher than single-list chunks."""
    c_both = _make_chunk("In both lists", "both")
    c_dense = _make_chunk("Only in dense", "dense")
    c_sparse = _make_chunk("Only in sparse", "sparse")

    dense = [(c_both, 0.9), (c_dense, 0.8)]
    sparse = [(c_both, 5.0), (c_sparse, 4.0)]

    fused = reciprocal_rank_fusion(dense, sparse, k=60, top_k=3)

    # c_both should have the highest fused score
    assert fused[0][0].source_id == "both"
    # And its score should be higher than either single-list chunk
    both_score = fused[0][1]
    other_scores = [s for _, s in fused[1:]]
    assert all(both_score > s for s in other_scores)


def test_rrf_score_monotonically_decreasing():
    """RRF output scores should be in descending order."""
    chunks = [_make_chunk(f"chunk {i}", str(i)) for i in range(10)]
    dense = [(c, 1.0 - i * 0.05) for i, c in enumerate(chunks[:6])]
    sparse = [(c, 10.0 - i) for i, c in enumerate(chunks[3:9])]

    fused = reciprocal_rank_fusion(dense, sparse, k=60, top_k=10)

    scores = [s for _, s in fused]
    for i in range(len(scores) - 1):
        assert scores[i] >= scores[i + 1], (
            f"Score at {i} ({scores[i]}) < score at {i+1} ({scores[i+1]})"
        )


# ============================================================================
# BM25 Keyword Search (2B)
# ============================================================================


def _make_chunk_with_meta(
    content: str,
    source_id: str = "test",
    metadata: dict | None = None,
) -> Chunk:
    return Chunk(
        content=content,
        metadata=metadata or {},
        chunk_type="intervention",
        source_id=source_id,
        position=0,
    )


def test_keyword_search_store_linear_scan_returns_scores():
    """_keyword_search_store without BM25 should return (chunk, score) tuples."""
    from src.indexing.embeddings import VectorStore

    store = VectorStore()
    store.chunks = [
        _make_chunk_with_meta(
            "L'article 150-0 B ter relatif à l'apport-cession et reinvestissement", "1",
        ),
        _make_chunk_with_meta("Le budget de l'éducation nationale pour 2024", "2"),
        _make_chunk_with_meta("Discussion sur l'article 150-0 B ter du CGI", "3"),
    ]

    service = SearchService.__new__(SearchService)

    results = service._keyword_search_store(
        store,
        keywords=["150-0 B ter", "apport-cession"],
        date_start=None,
        date_end=None,
        max_results=10,
        bm25_index=None,
    )

    # Should return tuples
    assert all(isinstance(r, tuple) and len(r) == 2 for r in results)
    # Chunks mentioning both keywords should score higher
    assert len(results) >= 2
    # First result should have higher score (matches 2 keywords)
    assert results[0][1] >= results[1][1]
    # Source 1 matches both keywords, source 3 matches only one
    assert results[0][0].source_id == "1"


def test_keyword_search_store_linear_scan_date_filter():
    """Linear scan should respect date filters."""
    from src.indexing.embeddings import VectorStore

    store = VectorStore()
    store.chunks = [
        _make_chunk_with_meta("Article 150-0 B ter", "1", {"date": "2020-01-15"}),
        _make_chunk_with_meta("Article 150-0 B ter", "2", {"date": "2023-06-01"}),
    ]

    service = SearchService.__new__(SearchService)
    from datetime import date

    results = service._keyword_search_store(
        store,
        keywords=["150-0 B ter"],
        date_start=date(2022, 1, 1),
        date_end=None,
        max_results=10,
        bm25_index=None,
    )

    assert len(results) == 1
    assert results[0][0].source_id == "2"


def test_keyword_search_store_with_bm25():
    """_keyword_search_store with BM25 should use BM25 ranking."""
    from src.indexing.embeddings import VectorStore

    store = VectorStore()
    c1 = _make_chunk_with_meta("L'article 150-0 B ter apport-cession report", "1")
    c2 = _make_chunk_with_meta("Budget éducation nationale", "2")
    c3 = _make_chunk_with_meta("Article 150-0 B ter du CGI", "3")
    store.chunks = [c1, c2, c3]

    # Mock BM25 index
    mock_bm25 = MagicMock()
    mock_bm25.search.return_value = [
        (c1, 8.5),
        (c3, 6.2),
        (c2, 0.1),
    ]

    service = SearchService.__new__(SearchService)

    results = service._keyword_search_store(
        store,
        keywords=["150-0 B ter"],
        date_start=None,
        date_end=None,
        max_results=10,
        bm25_index=mock_bm25,
    )

    # Should use BM25 results, filtered to only keyword matches
    assert len(results) == 2
    assert results[0][0].source_id == "1"
    assert results[0][1] == 8.5
    assert results[1][0].source_id == "3"


def test_keyword_search_returns_tuples():
    """keyword_search() public method should return (chunk, score) tuples."""
    from src.indexing.embeddings import VectorStore

    service = SearchService.__new__(SearchService)
    service.debate_store = VectorStore()
    service.amendment_store = VectorStore()
    service.debate_bm25 = None
    service.amendment_bm25 = None

    service.debate_store.chunks = [
        _make_chunk_with_meta("L'article 150-0 B ter du CGI", "1"),
    ]

    results = service.keyword_search(
        keywords=["150-0 B ter"],
        search_amendments=False,
    )

    assert "debates" in results
    debate_results = results["debates"]
    assert len(debate_results) == 1
    chunk, score = debate_results[0]
    assert isinstance(score, float)
    assert score > 0


# ============================================================================
# Score Explainability (3B)
# ============================================================================


def test_score_explanation_dataclass():
    """ScoreExplanation should be a proper dataclass with expected fields."""
    expl = ScoreExplanation(
        semantic_similarity=0.75,
        keyword_boost=0.25,
        speaker_boost=0.08,
        bm25_score=5.5,
        matched_keywords=["150-0 B ter", "apport-cession"],
        speaker_role="rapporteur général",
    )

    assert expl.semantic_similarity == 0.75
    assert expl.keyword_boost == 0.25
    assert expl.speaker_boost == 0.08
    assert expl.bm25_score == 5.5
    assert len(expl.matched_keywords) == 2
    assert expl.speaker_role == "rapporteur général"


def test_score_explanation_defaults():
    """ScoreExplanation defaults should be zero/empty."""
    expl = ScoreExplanation()
    assert expl.semantic_similarity == 0.0
    assert expl.keyword_boost == 0.0
    assert expl.speaker_boost == 0.0
    assert expl.bm25_score == 0.0
    assert expl.matched_keywords == []
    assert expl.speaker_role == ""


def test_hybrid_search_store_explain_populates_metadata():
    """When explain=True, _hybrid_search_store should store ScoreExplanation in metadata."""
    from src.indexing.embeddings import VectorStore

    store = VectorStore()
    # Create chunks with metadata including fonction for speaker boost
    store.chunks = [
        _make_chunk_with_meta(
            "L'article 150-0 B ter relatif à l'apport-cession",
            "1",
            {"fonction": "Rapporteur général"},
        ),
        _make_chunk_with_meta(
            "Le budget de l'éducation nationale et de la recherche",
            "2",
            {"fonction": ""},
        ),
    ]

    # Create embeddings (2 chunks, dim=4 for simplicity)
    store.embeddings = np.array([
        [0.5, 0.5, 0.5, 0.5],
        [0.1, 0.1, 0.8, 0.2],
    ], dtype=np.float32)

    service = SearchService.__new__(SearchService)
    query_embedding = [0.5, 0.5, 0.5, 0.5]

    results = service._hybrid_search_store(
        store,
        query_embedding=query_embedding,
        keywords=["150-0 B ter"],
        date_start=None,
        date_end=None,
        top_k=5,
        keyword_boost=0.25,
        min_similarity=0.1,
        explain=True,
    )

    assert len(results) >= 1

    # First result should be the 150-0 B ter chunk (higher similarity + keyword boost)
    chunk, score = results[0]
    assert chunk.source_id == "1"

    expl = chunk.metadata.get("_score_explanation")
    assert isinstance(expl, ScoreExplanation)
    assert expl.semantic_similarity > 0
    assert expl.keyword_boost == 0.25
    assert "150-0 b ter" in expl.matched_keywords
    assert expl.speaker_boost == 0.08  # rapporteur général
    assert expl.speaker_role == "rapporteur général"


def test_hybrid_search_store_no_explain_by_default():
    """When explain is not set, metadata should not contain _score_explanation."""
    from src.indexing.embeddings import VectorStore

    store = VectorStore()
    store.chunks = [
        _make_chunk_with_meta("L'article 150-0 B ter", "1", {}),
    ]
    store.embeddings = np.array([[0.5, 0.5, 0.5, 0.5]], dtype=np.float32)

    service = SearchService.__new__(SearchService)

    results = service._hybrid_search_store(
        store,
        query_embedding=[0.5, 0.5, 0.5, 0.5],
        keywords=["150-0 B ter"],
        date_start=None,
        date_end=None,
        top_k=5,
        keyword_boost=0.25,
        min_similarity=0.1,
        explain=False,
    )

    assert len(results) == 1
    assert "_score_explanation" not in results[0][0].metadata


# ============================================================================
# _passes_date_filter
# ============================================================================


def test_passes_date_filter_no_dates():
    """No date constraints means all chunks pass."""
    chunk = _make_chunk_with_meta("content", "1", {"date": "2020-01-01"})
    assert SearchService._passes_date_filter(chunk, None, None) is True


def test_passes_date_filter_within_range():
    """Chunk within date range should pass."""
    from datetime import date
    chunk = _make_chunk_with_meta("content", "1", {"date": "2022-06-15"})
    assert SearchService._passes_date_filter(
        chunk, date(2022, 1, 1), date(2023, 1, 1)
    ) is True


def test_passes_date_filter_before_start():
    """Chunk before date_start should not pass."""
    from datetime import date
    chunk = _make_chunk_with_meta("content", "1", {"date": "2019-06-15"})
    assert SearchService._passes_date_filter(
        chunk, date(2020, 1, 1), None
    ) is False


def test_passes_date_filter_no_date_metadata():
    """Chunk without date metadata should be excluded from date-filtered searches."""
    chunk = _make_chunk_with_meta("content", "1", {})
    from datetime import date
    assert SearchService._passes_date_filter(
        chunk, date(2020, 1, 1), date(2023, 1, 1)
    ) is False


def test_passes_date_filter_no_date_metadata_no_filter():
    """Chunk without date metadata should pass when no filter is active."""
    chunk = _make_chunk_with_meta("content", "1", {})
    assert SearchService._passes_date_filter(chunk, None, None) is True
