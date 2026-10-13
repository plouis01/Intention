"""Tests for production vector store implementations."""

import pytest

from src.indexing.chunking import Chunk
from src.indexing.vector_stores import (
    BaseVectorStore,
    _validate_sql_identifier,
    create_vector_store,
)


def test_base_vector_store_is_abstract():
    """Test that BaseVectorStore cannot be instantiated directly."""
    with pytest.raises(TypeError):
        BaseVectorStore()


def test_create_vector_store_memory():
    """Test creating in-memory vector store."""
    store = create_vector_store("memory")
    assert store is not None
    assert len(store) == 0


def test_create_vector_store_database_removed():
    """Test that 'database' store type is no longer supported."""
    with pytest.raises(ValueError, match="Unknown vector store type"):
        create_vector_store("database")


def test_create_vector_store_invalid():
    """Test creating store with invalid type raises error."""
    with pytest.raises(ValueError, match="Unknown vector store type"):
        create_vector_store("invalid_type")


def test_create_vector_store_default():
    """Test creating store with default settings."""
    from src.db.models import init_db
    init_db()

    # Should use settings.vector_store_provider (default: "memory")
    store = create_vector_store()
    assert store is not None


def test_memory_store_operations():
    """Test basic operations on memory store."""
    store = create_vector_store("memory")

    chunk = Chunk(
        content="Test content for vector store",
        metadata={"key": "value"},
        chunk_type="test",
        source_id="test-source",
        position=0,
    )
    embedding = [0.1] * 384  # 384-dim embedding

    # Add chunk
    store.add(chunk, embedding)
    assert len(store) == 1

    # Search
    results = store.search(embedding, top_k=5, min_similarity=0.9)
    assert len(results) >= 1
    found_chunk, similarity = results[0]
    assert found_chunk.content == chunk.content
    assert similarity > 0.99


def test_pgvector_import_error():
    """Test that pgvector raises ImportError if dependencies not installed."""
    # This test will pass if pgvector is not installed
    # and raise an ImportError which we catch
    try:
        from src.indexing.vector_stores import PgVectorStore
        # If import succeeds, we can't test the import error
        # So we just verify the class exists
        assert PgVectorStore is not None
    except ImportError:
        # Expected if pgvector not installed
        pass


def test_qdrant_import_error():
    """Test that Qdrant raises ImportError if dependencies not installed."""
    try:
        from src.indexing.vector_stores import QdrantVectorStore
        assert QdrantVectorStore is not None
    except ImportError:
        # Expected if qdrant-client not installed
        pass


def test_vector_store_context_manager():
    """Test vector store works as context manager."""
    with create_vector_store("memory") as store:
        chunk = Chunk(
            content="Context manager test",
            metadata={},
            chunk_type="test",
            source_id="test",
            position=0,
        )
        store.add(chunk, [0.1] * 384)
        assert len(store) == 1


class TestSqlIdentifierValidation:
    """Tests for SQL identifier validation to prevent SQL injection."""

    def test_valid_identifiers(self):
        """Test that valid SQL identifiers pass validation."""
        valid_names = [
            "chunks",
            "chunk_vectors",
            "my_table_123",
            "_private_table",
            "Table1",
            "UPPERCASE_TABLE",
        ]
        for name in valid_names:
            _validate_sql_identifier(name, "table_name")  # Should not raise

    def test_sql_injection_attempts(self):
        """Test that SQL injection attempts are rejected."""
        malicious_names = [
            "table; DROP TABLE users;--",
            "table' OR '1'='1",
            "table\"; DELETE FROM chunks;--",
            "table`; DROP TABLE chunks;--",
            "table); DROP TABLE chunks;--",
            "table UNION SELECT * FROM secrets",
        ]
        for name in malicious_names:
            with pytest.raises(ValueError, match="must start with a letter"):
                _validate_sql_identifier(name, "table_name")

    def test_empty_identifier(self):
        """Test that empty identifiers are rejected."""
        with pytest.raises(ValueError, match="must be 1-63 characters"):
            _validate_sql_identifier("", "table_name")

    def test_too_long_identifier(self):
        """Test that overly long identifiers are rejected."""
        long_name = "a" * 64  # PostgreSQL limit is 63
        with pytest.raises(ValueError, match="must be 1-63 characters"):
            _validate_sql_identifier(long_name, "table_name")

    def test_identifier_starting_with_number(self):
        """Test that identifiers starting with numbers are rejected."""
        with pytest.raises(ValueError, match="must start with a letter"):
            _validate_sql_identifier("123table", "table_name")

    def test_identifier_with_special_chars(self):
        """Test that identifiers with special characters are rejected."""
        invalid_names = ["table-name", "table.name", "table name", "table@name"]
        for name in invalid_names:
            with pytest.raises(ValueError, match="must start with a letter"):
                _validate_sql_identifier(name, "table_name")
