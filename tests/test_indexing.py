"""Tests for Phase 2 indexing modules."""

import pytest
from datetime import datetime

from src.indexing.chunking import (
    Chunk,
    chunk_by_intervention,
    chunk_by_sliding_window,
    chunk_amendment,
    estimate_tokens,
)
from src.parsers.debates import Intervention, ParsedDebat


def test_chunk_dataclass():
    """Test Chunk dataclass creation."""
    chunk = Chunk(
        content="Test content",
        metadata={"key": "value"},
        chunk_type="test",
        source_id="test-123",
        position=0,
    )

    assert chunk.content == "Test content"
    assert chunk.metadata["key"] == "value"
    assert chunk.chunk_type == "test"


def test_chunk_by_intervention():
    """Test chunking a debate by speaker interventions."""
    debat = ParsedDebat(
        chambre="AN",
        date=datetime(2024, 1, 15),
        etape="seance_publique",
        url_source=None,
        reference="test-debat",
        interventions=[
            Intervention(
                orateur="M. Dupont",
                fonction="rapporteur",
                contenu="Cet article vise à modifier le régime fiscal des plus-values immobilières.",
                articles_mentionnes=["12"],
                ordre=1,
            ),
            Intervention(
                orateur="Mme Martin",
                fonction="ministre",
                contenu="Le gouvernement soutient cette mesure qui permettra de simplifier le code général des impôts.",
                articles_mentionnes=["12"],
                ordre=2,
            ),
        ],
        contenu_integral="Full debate text...",
    )

    chunks = list(chunk_by_intervention(debat))

    assert len(chunks) == 2
    assert chunks[0].metadata["orateur"] == "M. Dupont"
    assert chunks[0].metadata["fonction"] == "rapporteur"
    assert chunks[0].metadata["chambre"] == "AN"
    assert "12" in chunks[0].metadata["articles_mentionnes"]


def test_chunk_by_sliding_window():
    """Test sliding window chunking."""
    text = "A" * 500 + ". " + "B" * 500 + ". " + "C" * 500

    chunks = list(chunk_by_sliding_window(
        text,
        window_size=600,
        overlap=100,
        metadata={"test": True},
        source_id="test",
    ))

    assert len(chunks) >= 2
    assert all(chunk.metadata["test"] for chunk in chunks)


def test_chunk_amendment():
    """Test amendment chunking."""
    chunk = chunk_amendment(
        numero="123",
        auteur="M. Dupont",
        objet="Modification du taux",
        contenu="Remplacer 20% par 25%",
        expose_motifs="Pour augmenter les recettes",
        sort="adopte",
        article_ref="15",
        chambre="AN",
        dossier_ref="DLR123",
        date="2023-10-15",
    )

    assert chunk.chunk_type == "amendement"
    assert "123" in chunk.metadata["numero"]
    assert "adopte" in chunk.metadata["sort"]
    assert chunk.metadata["date"] == "2023-10-15"
    assert "15" in chunk.content


def test_estimate_tokens():
    """Test token estimation."""
    text = "Bonjour le monde"  # 16 chars
    tokens = estimate_tokens(text)

    # ~4 chars per token
    assert 3 <= tokens <= 5


def test_import_embeddings_module():
    """Test that embeddings module can be imported."""
    from src.indexing.embeddings import (
        EmbeddingService,
        VectorStore,
        cosine_similarity,
        find_similar_chunks,
    )

    assert EmbeddingService is not None
    assert VectorStore is not None


def test_vector_store_basic():
    """Test basic VectorStore operations."""
    from src.indexing.embeddings import VectorStore

    store = VectorStore()

    chunk = Chunk(
        content="Test",
        metadata={},
        chunk_type="test",
        source_id="test",
        position=0,
    )
    embedding = [0.1, 0.2, 0.3]

    store.add(chunk, embedding)

    assert len(store) == 1


def test_cosine_similarity():
    """Test cosine similarity calculation."""
    from src.indexing.embeddings import cosine_similarity

    # Identical vectors
    sim = cosine_similarity([1, 0, 0], [1, 0, 0])
    assert abs(sim - 1.0) < 0.001

    # Orthogonal vectors
    sim = cosine_similarity([1, 0, 0], [0, 1, 0])
    assert abs(sim - 0.0) < 0.001

    # Opposite vectors
    sim = cosine_similarity([1, 0, 0], [-1, 0, 0])
    assert abs(sim - (-1.0)) < 0.001


def test_import_intent_extraction():
    """Test that intent extraction module can be imported."""
    from src.indexing.intent_extraction import (
        IntentExtractor,
        IntentResult,
        CertitudeLevel,
        format_intent_result_markdown,
    )

    assert IntentExtractor is not None
    assert CertitudeLevel.ELEVE.value == "élevé"


def test_import_search_module():
    """Test that search module can be imported."""
    from src.indexing.search import (
        SearchService,
        SearchResult,
        format_search_result_markdown,
    )

    assert SearchService is not None
    assert SearchResult is not None


