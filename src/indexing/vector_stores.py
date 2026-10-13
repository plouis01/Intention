"""Production vector store implementations.

Supports multiple backends:
- memory: In-memory store with JSON persistence (default, for development)
- pgvector: PostgreSQL with pgvector extension (recommended for production)
- qdrant: Qdrant vector database (for large-scale deployments)

Install optional dependencies:
    pip install intention-legislateur[pgvector]  # For pgvector
    pip install intention-legislateur[qdrant]    # For Qdrant
    pip install intention-legislateur[all-vectordb]  # For all vector DBs
"""

import logging
import re
from abc import ABC, abstractmethod

# Valid SQL identifier pattern: starts with letter/underscore, contains only alphanumeric/underscore
_VALID_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_sql_identifier(name: str, param_name: str = "identifier") -> None:
    """
    Validate that a string is a safe SQL identifier to prevent SQL injection.

    Args:
        name: The identifier to validate
        param_name: Name of the parameter (for error messages)

    Raises:
        ValueError: If the identifier contains invalid characters
    """
    if not name or len(name) > 63:  # PostgreSQL identifier limit
        raise ValueError(f"{param_name} must be 1-63 characters long")
    if not _VALID_IDENTIFIER_PATTERN.match(name):
        raise ValueError(
            f"{param_name} must start with a letter or underscore and contain only "
            f"alphanumeric characters and underscores. Got: {name!r}"
        )

from src.indexing.chunking import Chunk
from src.utils.config import get_settings

logger = logging.getLogger(__name__)


class BaseVectorStore(ABC):
    """Abstract base class for vector stores."""

    @abstractmethod
    def add(self, chunk: Chunk, embedding: list[float]) -> str | int:
        """Add a chunk with its embedding. Returns the ID."""
        pass

    @abstractmethod
    def add_batch(self, chunk_embeddings: list[tuple[Chunk, list[float]]]) -> int:
        """Add multiple chunks. Returns count of items added."""
        pass

    @abstractmethod
    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        min_similarity: float = 0.5,
        chunk_type: str | None = None,
    ) -> list[tuple[Chunk, float]]:
        """Search for similar chunks. Returns list of (chunk, similarity) tuples."""
        pass

    @abstractmethod
    def count(self, chunk_type: str | None = None) -> int:
        """Count chunks in the store."""
        pass

    @abstractmethod
    def delete_by_type(self, chunk_type: str) -> int:
        """Delete all chunks of a given type. Returns count deleted."""
        pass

    def close(self):
        """Close any open connections."""
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __len__(self) -> int:
        return self.count()


class PgVectorStore(BaseVectorStore):
    """
    PostgreSQL vector store using pgvector extension.

    Requires:
    - PostgreSQL with pgvector extension installed
    - pip install pgvector psycopg2-binary

    Setup PostgreSQL:
        CREATE EXTENSION IF NOT EXISTS vector;

    The store creates a table 'chunk_vectors' with:
    - id: Serial primary key
    - chunk_type: Type of chunk (intervention, amendement, etc.)
    - content: Original text content
    - content_hash: SHA256 hash for deduplication
    - chunk_metadata: JSON metadata
    - source_id: Reference to source
    - position: Order within source
    - embedding: vector(dimension)
    - created_at: Timestamp
    """

    def __init__(
        self,
        connection_string: str | None = None,
        embedding_dim: int = 384,
        table_name: str = "chunk_vectors",
    ):
        """
        Initialize pgvector store.

        Args:
            connection_string: PostgreSQL connection string. If None, uses DATABASE_URL.
            embedding_dim: Dimension of embedding vectors (default 384 for MiniLM)
            table_name: Name of the table to use
        """
        try:
            import psycopg2
            from pgvector.psycopg2 import register_vector
        except ImportError:
            raise ImportError(
                "pgvector support requires additional dependencies. "
                "Install with: pip install intention-legislateur[pgvector]"
            )

        # Validate table_name to prevent SQL injection
        _validate_sql_identifier(table_name, "table_name")

        self.settings = get_settings()
        self.connection_string = connection_string or self.settings.effective_database_url
        self.embedding_dim = embedding_dim
        self.table_name = table_name

        # Connect to PostgreSQL
        self._conn = psycopg2.connect(self.connection_string)
        register_vector(self._conn)

        # Create table if not exists
        self._create_table()

    def _create_table(self):
        """Create the vector table if it doesn't exist."""
        with self._conn.cursor() as cur:
            # Enable pgvector extension
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")

            # Create table
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.table_name} (
                    id SERIAL PRIMARY KEY,
                    chunk_type VARCHAR(50) NOT NULL,
                    content TEXT NOT NULL,
                    content_hash VARCHAR(64) NOT NULL UNIQUE,
                    chunk_metadata JSONB,
                    source_id VARCHAR(100),
                    position INTEGER,
                    embedding vector({self.embedding_dim}) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create indexes
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self.table_name}_type
                ON {self.table_name} (chunk_type)
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self.table_name}_hash
                ON {self.table_name} (content_hash)
            """)
            # Create HNSW index for fast similarity search
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self.table_name}_embedding
                ON {self.table_name}
                USING hnsw (embedding vector_cosine_ops)
            """)

            self._conn.commit()
            logger.info(f"pgvector table '{self.table_name}' ready")

    def _compute_hash(self, content: str) -> str:
        """Compute content hash for deduplication."""
        import hashlib
        return hashlib.sha256(content.encode()).hexdigest()

    def add(self, chunk: Chunk, embedding: list[float]) -> int:
        """Add a chunk with its embedding."""
        import json

        content_hash = self._compute_hash(chunk.content)

        with self._conn.cursor() as cur:
            # Check for existing
            cur.execute(
                f"SELECT id FROM {self.table_name} WHERE content_hash = %s",
                (content_hash,)
            )
            existing = cur.fetchone()
            if existing:
                return existing[0]

            # Insert new
            cur.execute(
                f"""
                INSERT INTO {self.table_name}
                (chunk_type, content, content_hash, chunk_metadata, source_id, position, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    chunk.chunk_type,
                    chunk.content,
                    content_hash,
                    json.dumps(chunk.metadata),
                    chunk.source_id,
                    chunk.position,
                    embedding,
                )
            )
            record_id = cur.fetchone()[0]
            self._conn.commit()
            return record_id

    def add_batch(self, chunk_embeddings: list[tuple[Chunk, list[float]]]) -> int:
        """Add multiple chunks efficiently."""
        import json
        from psycopg2.extras import execute_values

        # Prepare data, skipping duplicates
        data = []
        seen_hashes = set()

        for chunk, embedding in chunk_embeddings:
            content_hash = self._compute_hash(chunk.content)
            if content_hash in seen_hashes:
                continue
            seen_hashes.add(content_hash)

            data.append((
                chunk.chunk_type,
                chunk.content,
                content_hash,
                json.dumps(chunk.metadata),
                chunk.source_id,
                chunk.position,
                embedding,
            ))

        if not data:
            return 0

        with self._conn.cursor() as cur:
            # Use ON CONFLICT to handle duplicates
            execute_values(
                cur,
                f"""
                INSERT INTO {self.table_name}
                (chunk_type, content, content_hash, chunk_metadata, source_id, position, embedding)
                VALUES %s
                ON CONFLICT (content_hash) DO NOTHING
                """,
                data,
                template="(%s, %s, %s, %s, %s, %s, %s)"
            )
            inserted = cur.rowcount
            self._conn.commit()

        logger.info(f"Inserted {inserted} chunks (skipped {len(chunk_embeddings) - inserted} duplicates)")
        return inserted

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        min_similarity: float = 0.5,
        chunk_type: str | None = None,
    ) -> list[tuple[Chunk, float]]:
        """Search for similar chunks using cosine similarity."""
        with self._conn.cursor() as cur:
            type_filter = "AND chunk_type = %s" if chunk_type else ""

            cur.execute(
                f"""
                SELECT
                    chunk_type, content, chunk_metadata, source_id, position,
                    1 - (embedding <=> %s) as similarity
                FROM {self.table_name}
                WHERE 1 - (embedding <=> %s) >= %s
                {type_filter}
                ORDER BY embedding <=> %s
                LIMIT %s
                """,
                [query_embedding, query_embedding, min_similarity]
                + ([chunk_type] if chunk_type else [])
                + [query_embedding, top_k]
            )

            results = []
            for row in cur.fetchall():
                chunk = Chunk(
                    content=row[1],
                    metadata=row[2] or {},
                    chunk_type=row[0],
                    source_id=row[3] or "",
                    position=row[4] or 0,
                )
                results.append((chunk, float(row[5])))

            return results

    def count(self, chunk_type: str | None = None) -> int:
        """Count chunks in the store."""
        with self._conn.cursor() as cur:
            if chunk_type:
                cur.execute(
                    f"SELECT COUNT(*) FROM {self.table_name} WHERE chunk_type = %s",
                    (chunk_type,)
                )
            else:
                cur.execute(f"SELECT COUNT(*) FROM {self.table_name}")
            return cur.fetchone()[0]

    def delete_by_type(self, chunk_type: str) -> int:
        """Delete all chunks of a given type."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {self.table_name} WHERE chunk_type = %s",
                (chunk_type,)
            )
            deleted = cur.rowcount
            self._conn.commit()
            logger.info(f"Deleted {deleted} chunks of type '{chunk_type}'")
            return deleted

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None


class ChunkEmbeddingsPgVectorStore(BaseVectorStore):
    """
    PostgreSQL vector store using the chunk_embeddings table with embedding_vector column.

    The embedding pipeline writes directly to 'embedding_vector' (pgvector format).
    This store is read-only (searches only).
    """

    def __init__(
        self,
        connection_string: str | None = None,
        embedding_dim: int = 384,
    ):
        try:
            import psycopg2
            import psycopg2.pool
            from pgvector.psycopg2 import register_vector
        except ImportError:
            raise ImportError(
                "pgvector support requires additional dependencies. "
                "Install with: pip install psycopg2-binary pgvector"
            )

        self.settings = get_settings()
        self.connection_string = connection_string or self.settings.effective_database_url
        self.embedding_dim = embedding_dim

        # Use a connection pool for thread safety.
        # Each search() call gets its own connection from the pool.
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=20,
            dsn=self.connection_string,
        )

        # Register pgvector type on a test connection to validate setup
        test_conn = self._pool.getconn()
        try:
            register_vector(test_conn)
        finally:
            self._pool.putconn(test_conn)

        self._register_vector = register_vector
        logger.info("Connected to chunk_embeddings table for pgvector search")

    def _get_conn(self):
        """Get a connection from the pool with pgvector registered."""
        conn = self._pool.getconn()
        try:
            self._register_vector(conn)
        except Exception:
            self._pool.putconn(conn)
            raise
        return conn

    def _put_conn(self, conn):
        """Return a connection to the pool."""
        self._pool.putconn(conn)

    def add(self, chunk: Chunk, embedding: list[float]) -> int:
        raise NotImplementedError(
            "ChunkEmbeddingsPgVectorStore is read-only. "
            "Use the embedding pipeline to add chunks."
        )

    def add_batch(self, chunk_embeddings: list[tuple[Chunk, list[float]]]) -> int:
        raise NotImplementedError(
            "ChunkEmbeddingsPgVectorStore is read-only. "
            "Use the embedding pipeline to add chunks."
        )

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        min_similarity: float = 0.5,
        chunk_type: str | None = None,
    ) -> list[tuple[Chunk, float]]:
        """Search for similar chunks using pgvector HNSW index on embedding_vector."""
        import json
        import numpy as np

        # Normalize embedding to list[float] (handles numpy arrays)
        if isinstance(query_embedding, np.ndarray):
            query_embedding = query_embedding.tolist()

        conn = self._get_conn()
        try:
            # Set autocommit only if not already in a transaction
            if not conn.autocommit:
                try:
                    conn.autocommit = True
                except Exception:
                    # Already in transaction, commit it first then set autocommit
                    conn.commit()
                    conn.autocommit = True

            with conn.cursor() as cur:
                # Configure IVFFlat search parameters (we're using IVFFlat, not HNSW)
                try:
                    cur.execute("SET ivfflat.probes = 40")
                except Exception:
                    # Ignore if parameter doesn't exist
                    pass

                type_filter = "AND chunk_type = %s" if chunk_type else ""

                # Convert query_embedding to pgvector format string
                # pgvector needs '[1,2,3]' format, not a Python list
                vector_str = '[' + ','.join(str(x) for x in query_embedding) + ']'

                # Use ORDER BY + LIMIT for HNSW/IVFFlat index, then post-filter by similarity
                query_sql = f"""
                    SELECT chunk_type, content, chunk_metadata, source_id, position,
                           1 - (embedding_vector <=> %s::vector) as similarity
                    FROM chunk_embeddings
                    WHERE embedding_vector IS NOT NULL
                      AND 1 - (embedding_vector <=> %s::vector) >= %s
                      {type_filter}
                    ORDER BY embedding_vector <=> %s::vector
                    LIMIT %s
                """

                params = [vector_str, vector_str, min_similarity]
                if chunk_type:
                    params.append(chunk_type)
                params.extend([vector_str, top_k])

                cur.execute(query_sql, params)

                results = []
                for row in cur.fetchall():
                    metadata = row[2]
                    if isinstance(metadata, str):
                        metadata = json.loads(metadata) if metadata else {}
                    elif metadata is None:
                        metadata = {}

                    chunk = Chunk(
                        content=row[1],
                        metadata=metadata,
                        chunk_type=row[0],
                        source_id=row[3] or "",
                        position=row[4] or 0,
                    )
                    results.append((chunk, float(row[5])))

                return results
        except Exception:
            # Reset connection state on error to avoid InFailedSqlTransaction
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._put_conn(conn)

    def search_with_text_filter(
        self,
        query_embedding: list[float],
        text_filters: list[str],
        top_k: int = 10,
        min_similarity: float = 0.3,
        chunk_type: str | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
    ) -> list[tuple[Chunk, float]]:
        """Search combining vector similarity with text content filtering.

        Runs two queries: one with text filter (high precision) and one
        pure vector search (broader recall), then merges results.

        Args:
            date_start: ISO date string for filtering (e.g., "2019-09-01")
            date_end: ISO date string for filtering (e.g., "2020-01-31")
        """
        import json
        import numpy as np

        if isinstance(query_embedding, np.ndarray):
            query_embedding = query_embedding.tolist()

        conn = self._get_conn()
        try:
            if not conn.autocommit:
                try:
                    conn.autocommit = True
                except Exception:
                    conn.commit()
                    conn.autocommit = True

            vector_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

            with conn.cursor() as cur:
                try:
                    cur.execute("SET ivfflat.probes = 40")
                except Exception:
                    pass

                # Build text filter conditions
                text_conditions = " OR ".join(
                    "content ILIKE %s" for _ in text_filters
                )
                text_params = [f"%{t}%" for t in text_filters]

                type_filter = "AND chunk_type = %s" if chunk_type else ""

                # Build date filter conditions (pushed to SQL for efficiency)
                date_filter = ""
                date_params = []
                if date_start:
                    date_filter += " AND chunk_metadata->>'date' >= %s"
                    date_params.append(date_start)
                if date_end:
                    date_filter += " AND chunk_metadata->>'date' <= %s"
                    date_params.append(date_end)

                # Query 1: Vector search WITH text filter (high precision)
                text_sql = f"""
                    SELECT chunk_type, content, chunk_metadata,
                           source_id, position,
                           1 - (embedding_vector <=> %s::vector) as sim
                    FROM chunk_embeddings
                    WHERE embedding_vector IS NOT NULL
                      AND ({text_conditions})
                      {type_filter}
                      {date_filter}
                    ORDER BY embedding_vector <=> %s::vector
                    LIMIT %s
                """
                params1 = [vector_str] + text_params
                if chunk_type:
                    params1.append(chunk_type)
                params1.extend(date_params)
                params1.extend([vector_str, top_k])
                cur.execute(text_sql, params1)
                text_results = cur.fetchall()

                # Query 2: Pure vector search (broader recall)
                pure_sql = f"""
                    SELECT chunk_type, content, chunk_metadata,
                           source_id, position,
                           1 - (embedding_vector <=> %s::vector) as sim
                    FROM chunk_embeddings
                    WHERE embedding_vector IS NOT NULL
                      AND 1 - (embedding_vector <=> %s::vector) >= %s
                      {type_filter}
                      {date_filter}
                    ORDER BY embedding_vector <=> %s::vector
                    LIMIT %s
                """
                params2 = [vector_str, vector_str, min_similarity]
                if chunk_type:
                    params2.append(chunk_type)
                params2.extend(date_params)
                params2.extend([vector_str, top_k])
                cur.execute(pure_sql, params2)
                pure_results = cur.fetchall()

                # Merge: text-filtered results first (guaranteed slots), then pure vector
                seen = set()
                results = []

                def _row_to_chunk(row):
                    metadata = row[2]
                    if isinstance(metadata, str):
                        metadata = json.loads(metadata) if metadata else {}
                    elif metadata is None:
                        metadata = {}
                    return Chunk(
                        content=row[1],
                        metadata=metadata,
                        chunk_type=row[0],
                        source_id=row[3] or "",
                        position=row[4] or 0,
                    )

                # Text-filtered results contain the article reference;
                # pure vector results capture topical relevance.
                # Reserve at least 1/3 of slots for pure vector to avoid
                # short/ambiguous refs (e.g. "39") crowding out topic matches.
                text_chunks = []
                for row in text_results:
                    key = (row[3], row[4])  # (source_id, position)
                    if key not in seen:
                        seen.add(key)
                        text_chunks.append((_row_to_chunk(row), float(row[5]) + 0.15))

                # Sort text-matched by similarity
                text_chunks.sort(key=lambda x: x[1], reverse=True)
                max_text_slots = top_k * 2 // 3  # Reserve 1/3 for pure vector
                results.extend(text_chunks[:max_text_slots])

                # Fill remaining slots with pure vector results
                remaining = top_k - len(results)
                vector_chunks = []
                for row in pure_results:
                    key = (row[3], row[4])
                    if key not in seen:
                        seen.add(key)
                        vector_chunks.append((_row_to_chunk(row), float(row[5])))

                vector_chunks.sort(key=lambda x: x[1], reverse=True)
                results.extend(vector_chunks[:max(remaining, 0)])
                return results
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._put_conn(conn)

    def text_only_search(
        self,
        text_filters: list[str],
        top_k: int = 30,
        chunk_type: str | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
    ) -> list[tuple[Chunk, float]]:
        """ILIKE-only search without vector similarity — used as fallback when BM25 is unavailable.

        Returns chunks matching any text filter, ordered by date (most recent first).
        Scores are synthetic (1.0 for all results) since no ranking is available.
        """
        import json

        conn = self._get_conn()
        try:
            if not conn.autocommit:
                try:
                    conn.autocommit = True
                except Exception:
                    conn.commit()
                    conn.autocommit = True

            with conn.cursor() as cur:
                text_conditions = " OR ".join(
                    "content ILIKE %s" for _ in text_filters
                )
                text_params = [f"%{t}%" for t in text_filters]

                type_filter = "AND chunk_type = %s" if chunk_type else ""

                date_filter = ""
                date_params = []
                if date_start:
                    date_filter += " AND chunk_metadata->>'date' >= %s"
                    date_params.append(date_start)
                if date_end:
                    date_filter += " AND chunk_metadata->>'date' <= %s"
                    date_params.append(date_end)

                sql = f"""
                    SELECT chunk_type, content, chunk_metadata,
                           source_id, position
                    FROM chunk_embeddings
                    WHERE ({text_conditions})
                      AND LENGTH(content) > 80
                      {type_filter}
                      {date_filter}
                    ORDER BY chunk_metadata->>'date' DESC
                    LIMIT %s
                """
                params = list(text_params)
                if chunk_type:
                    params.append(chunk_type)
                params.extend(date_params)
                params.append(top_k)

                cur.execute(sql, params)

                results = []
                for row in cur.fetchall():
                    metadata = row[2]
                    if isinstance(metadata, str):
                        metadata = json.loads(metadata) if metadata else {}
                    elif metadata is None:
                        metadata = {}
                    chunk = Chunk(
                        content=row[1],
                        metadata=metadata,
                        chunk_type=row[0],
                        source_id=row[3] or "",
                        position=row[4] or 0,
                    )
                    results.append((chunk, 1.0))

                return results
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._put_conn(conn)

    def count(self, chunk_type: str | None = None) -> int:
        """Count chunks with embedding_vector."""
        conn = self._get_conn()
        try:
            if not conn.autocommit:
                try:
                    conn.autocommit = True
                except Exception:
                    conn.commit()
                    conn.autocommit = True
            with conn.cursor() as cur:
                if chunk_type:
                    cur.execute(
                        "SELECT COUNT(*) FROM chunk_embeddings "
                        "WHERE chunk_type = %s AND embedding_vector IS NOT NULL",
                        (chunk_type,)
                    )
                else:
                    cur.execute(
                        "SELECT COUNT(*) FROM chunk_embeddings "
                        "WHERE embedding_vector IS NOT NULL"
                    )
                return cur.fetchone()[0]
        finally:
            self._put_conn(conn)

    def delete_by_type(self, chunk_type: str) -> int:
        raise NotImplementedError(
            "ChunkEmbeddingsPgVectorStore is read-only. "
            "Manage data through the embedding pipeline."
        )

    def close(self):
        """Close all connections in the pool."""
        if self._pool:
            self._pool.closeall()
            self._pool = None


class QdrantVectorStore(BaseVectorStore):
    """
    Qdrant vector store for large-scale deployments.

    Requires:
    - Qdrant server running (docker or cloud)
    - pip install qdrant-client

    Start Qdrant with Docker:
        docker run -p 6333:6333 qdrant/qdrant

    Features:
    - Horizontal scaling
    - Filtering by metadata
    - HNSW indexing for fast search
    - Cloud-hosted option available
    """

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        collection_name: str = "chunks",
        embedding_dim: int = 384,
    ):
        """
        Initialize Qdrant store.

        Args:
            url: Qdrant server URL. If None, uses QDRANT_URL from settings.
            api_key: Qdrant API key. If None, uses QDRANT_API_KEY from settings.
            collection_name: Name of the collection to use.
            embedding_dim: Dimension of embedding vectors.
        """
        try:
            from qdrant_client import QdrantClient
        except ImportError:
            raise ImportError(
                "Qdrant support requires additional dependencies. "
                "Install with: pip install intention-legislateur[qdrant]"
            )

        self.settings = get_settings()
        self.url = url or self.settings.qdrant_url
        self.api_key = api_key or self.settings.qdrant_api_key or None
        self.collection_name = collection_name
        self.embedding_dim = embedding_dim

        # Connect to Qdrant
        self._client = QdrantClient(
            url=self.url,
            api_key=self.api_key,
        )

        # Create collection if not exists
        self._ensure_collection()

    def _ensure_collection(self):
        """Create collection if it doesn't exist."""
        from qdrant_client.models import Distance, VectorParams

        collections = self._client.get_collections().collections
        exists = any(c.name == self.collection_name for c in collections)

        if not exists:
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(
                    size=self.embedding_dim,
                    distance=Distance.COSINE,
                ),
            )
            logger.info(f"Created Qdrant collection '{self.collection_name}'")
        else:
            logger.info(f"Using existing Qdrant collection '{self.collection_name}'")

    def _compute_hash(self, content: str) -> str:
        """Compute content hash for ID generation."""
        import hashlib
        return hashlib.sha256(content.encode()).hexdigest()

    def add(self, chunk: Chunk, embedding: list[float]) -> str:
        """Add a chunk with its embedding."""
        from qdrant_client.models import PointStruct

        point_id = self._compute_hash(chunk.content)

        point = PointStruct(
            id=point_id,
            vector=embedding,
            payload={
                "chunk_type": chunk.chunk_type,
                "content": chunk.content,
                "source_id": chunk.source_id,
                "position": chunk.position,
                **chunk.metadata,
            },
        )

        self._client.upsert(
            collection_name=self.collection_name,
            points=[point],
        )

        return point_id

    def add_batch(self, chunk_embeddings: list[tuple[Chunk, list[float]]]) -> int:
        """Add multiple chunks efficiently."""
        from qdrant_client.models import PointStruct

        points = []
        seen_ids = set()

        for chunk, embedding in chunk_embeddings:
            point_id = self._compute_hash(chunk.content)
            if point_id in seen_ids:
                continue
            seen_ids.add(point_id)

            points.append(PointStruct(
                id=point_id,
                vector=embedding,
                payload={
                    "chunk_type": chunk.chunk_type,
                    "content": chunk.content,
                    "source_id": chunk.source_id,
                    "position": chunk.position,
                    **chunk.metadata,
                },
            ))

        if not points:
            return 0

        # Batch upsert in chunks of 100
        batch_size = 100
        for i in range(0, len(points), batch_size):
            batch = points[i:i + batch_size]
            self._client.upsert(
                collection_name=self.collection_name,
                points=batch,
            )

        logger.info(f"Upserted {len(points)} chunks to Qdrant")
        return len(points)

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        min_similarity: float = 0.5,
        chunk_type: str | None = None,
    ) -> list[tuple[Chunk, float]]:
        """Search for similar chunks."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        # Build filter if chunk_type specified
        query_filter = None
        if chunk_type:
            query_filter = Filter(
                must=[
                    FieldCondition(
                        key="chunk_type",
                        match=MatchValue(value=chunk_type),
                    )
                ]
            )

        results = self._client.search(
            collection_name=self.collection_name,
            query_vector=query_embedding,
            query_filter=query_filter,
            limit=top_k,
            score_threshold=min_similarity,
        )

        chunks = []
        for hit in results:
            payload = hit.payload
            chunk = Chunk(
                content=payload.get("content", ""),
                metadata={
                    k: v for k, v in payload.items()
                    if k not in ("chunk_type", "content", "source_id", "position")
                },
                chunk_type=payload.get("chunk_type", ""),
                source_id=payload.get("source_id", ""),
                position=payload.get("position", 0),
            )
            chunks.append((chunk, hit.score))

        return chunks

    def count(self, chunk_type: str | None = None) -> int:
        """Count chunks in the collection."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        if chunk_type:
            query_filter = Filter(
                must=[
                    FieldCondition(
                        key="chunk_type",
                        match=MatchValue(value=chunk_type),
                    )
                ]
            )
            result = self._client.count(
                collection_name=self.collection_name,
                count_filter=query_filter,
            )
        else:
            result = self._client.count(collection_name=self.collection_name)

        return result.count

    def delete_by_type(self, chunk_type: str) -> int:
        """Delete all chunks of a given type."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        count_before = self.count(chunk_type)

        self._client.delete(
            collection_name=self.collection_name,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="chunk_type",
                        match=MatchValue(value=chunk_type),
                    )
                ]
            ),
        )

        logger.info(f"Deleted {count_before} chunks of type '{chunk_type}'")
        return count_before

    def close(self):
        """Close the client connection."""
        if self._client:
            self._client.close()
            self._client = None


def create_vector_store(
    store_type: str | None = None,
    collection_name: str = "chunks",
    embedding_dim: int = 384,
    **kwargs,
) -> BaseVectorStore:
    """
    Factory function to create the appropriate vector store.

    Args:
        store_type: Type of store ("memory", "pgvector", "qdrant").
                   If None, uses VECTOR_STORE_PROVIDER from settings.
        collection_name: Name for the collection/table.
        embedding_dim: Dimension of embedding vectors.
        **kwargs: Additional arguments passed to the store constructor.

    Returns:
        A vector store instance.

    Examples:
        # Use default from settings
        store = create_vector_store()

        # Use pgvector on chunk_embeddings table (recommended for production)
        store = create_vector_store("pgvector", embedding_dim=384)

        # Create Qdrant store with custom URL
        store = create_vector_store("qdrant", url="http://qdrant:6333")
    """
    settings = get_settings()
    store_type = store_type or settings.vector_store_provider

    if store_type == "memory":
        from src.indexing.embeddings import VectorStore
        logger.info("Using in-memory vector store")
        return VectorStore()

    elif store_type == "pgvector":
        logger.info("Using pgvector on chunk_embeddings table (HNSW search)")
        return ChunkEmbeddingsPgVectorStore(
            embedding_dim=embedding_dim,
            **kwargs,
        )

    elif store_type == "qdrant":
        logger.info("Using Qdrant vector store")
        return QdrantVectorStore(
            collection_name=collection_name,
            embedding_dim=embedding_dim,
            **kwargs,
        )

    else:
        raise ValueError(
            f"Unknown vector store type: {store_type}. "
            f"Supported types: memory, pgvector, qdrant"
        )
