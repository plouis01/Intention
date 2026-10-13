"""Embedding generation and vector storage service."""

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from src.indexing.chunking import Chunk
from src.utils.config import get_settings

logger = logging.getLogger(__name__)


def _compute_content_hash(content: str) -> str:
    """Compute SHA256 hash of content for deduplication."""
    return hashlib.sha256(content.encode()).hexdigest()


class EmbeddingService:
    """
    Service for generating and managing embeddings.

    Supports three providers:
    - "local": Uses sentence-transformers (free, runs locally)
    - "openai": Uses OpenAI API (requires OPENAI_API_KEY)
    - "vertex": Uses Google Vertex AI text-embedding-004 (requires gcloud auth)
    """

    def __init__(self, cache_dir: Path | None = None):
        self.settings = get_settings()
        self._openai_client = None
        self._local_model = None
        self._vertex_client = None
        self.cache_dir = cache_dir or Path("data/embeddings_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Determine provider
        self.provider = self.settings.embedding_provider

    @property
    def local_model(self):
        """Lazy-load local sentence-transformers model with ONNX backend."""
        if self._local_model is None:
            from sentence_transformers import SentenceTransformer

            model_name = self.settings.local_embedding_model
            try:
                logger.info(f"Loading local embedding model: {model_name} (ONNX backend)")
                self._local_model = SentenceTransformer(model_name, backend="onnx")
            except Exception:
                logger.info(f"ONNX unavailable, loading {model_name} with PyTorch backend")
                self._local_model = SentenceTransformer(model_name)
        return self._local_model

    @property
    def openai_client(self):
        """Lazy-load OpenAI client."""
        if self._openai_client is None:
            from openai import OpenAI

            if not self.settings.openai_api_key:
                raise ValueError(
                    "OpenAI API key not configured. Set OPENAI_API_KEY environment variable "
                    "or use EMBEDDING_PROVIDER=local for free local embeddings."
                )
            self._openai_client = OpenAI(api_key=self.settings.openai_api_key)
        return self._openai_client

    @property
    def vertex_client(self):
        """Lazy-load Google GenAI client for Vertex AI embeddings.

        Auth order:
        1. GOOGLE_APPLICATION_CREDENTIALS_JSON env var (JSON string, for Railway)
        2. GOOGLE_APPLICATION_CREDENTIALS_JSON_FILE env var (path to JSON file, for local .env)
        3. Application Default Credentials (gcloud auth application-default login)
        """
        if self._vertex_client is None:
            import json as json_mod
            import os

            from google import genai

            if not self.settings.vertex_project:
                raise ValueError(
                    "Vertex AI project not configured. Set VERTEX_PROJECT environment variable."
                )

            credentials = None
            creds_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
            if not creds_json:
                json_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON_FILE")
                if json_file and Path(json_file).exists():
                    creds_json = Path(json_file).read_text()
            if creds_json:
                from google.oauth2 import service_account
                info = json_mod.loads(creds_json)
                credentials = service_account.Credentials.from_service_account_info(
                    info,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )

            self._vertex_client = genai.Client(
                vertexai=True,
                project=self.settings.vertex_project,
                location=self.settings.vertex_location,
                credentials=credentials,
            )
        return self._vertex_client

    def _generate_vertex_embedding(self, text: str) -> list[float]:
        """Generate embedding using Vertex AI text-embedding-005."""
        max_chars = 6000
        if len(text) > max_chars:
            text = text[:max_chars]

        result = self.vertex_client.models.embed_content(
            model=self.settings.vertex_embedding_model,
            contents=text,
        )
        return result.embeddings[0].values

    def _create_vertex_client(self):
        """Create a new Vertex AI client (for use in thread pools)."""
        import json as json_mod
        import os

        from google import genai

        credentials = None
        creds_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        if not creds_json:
            json_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON_FILE")
            if json_file and Path(json_file).exists():
                creds_json = Path(json_file).read_text()
        if creds_json:
            from google.oauth2 import service_account
            info = json_mod.loads(creds_json)
            credentials = service_account.Credentials.from_service_account_info(
                info,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

        return genai.Client(
            vertexai=True,
            project=self.settings.vertex_project,
            location=self.settings.vertex_location,
            credentials=credentials,
        )

    def _embed_vertex_batch_with_retry(self, client, batch: list[str]) -> list[list[float]]:
        """Embed a single batch with retry on transient errors and split on token overflow."""
        import time as _time

        for attempt in range(5):
            try:
                result = client.models.embed_content(
                    model=self.settings.vertex_embedding_model,
                    contents=batch,
                )
                return [e.values for e in result.embeddings]
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "timed out" in err_str.lower() or "ReadTimeout" in type(e).__name__:
                    wait = 2 ** attempt
                    logger.warning(f"Retryable error, waiting {wait}s (attempt {attempt + 1}/5): {type(e).__name__}")
                    _time.sleep(wait)
                elif "400" in err_str or "INVALID_ARGUMENT" in err_str:
                    # Token limit or bad request - split or truncate
                    if len(batch) <= 1:
                        batch = [batch[0][:2000]]
                        logger.warning(f"Single text too large, truncating to 2000 chars")
                        continue
                    mid = len(batch) // 2
                    logger.warning(f"Batch of {len(batch)} got 400, splitting in half")
                    return self._embed_vertex_batch_with_retry(client, batch[:mid]) + \
                           self._embed_vertex_batch_with_retry(client, batch[mid:])
                else:
                    raise
        raise RuntimeError(f"Failed after 5 retries for batch of {len(batch)}")

    def _generate_vertex_embeddings_batch(
        self, texts: list[str], batch_size: int = 25, max_workers: int = 5
    ) -> list[list[float]]:
        """Generate embeddings in batch using Vertex AI with parallel requests.

        Each worker thread gets its own genai.Client to avoid httpx thread-safety issues.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed

        max_chars = 4000
        batches = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            batch = [t[:max_chars] if len(t) > max_chars else t for t in batch]
            batches.append((i, batch))

        # Thread-local clients
        local = threading.local()

        def _get_client():
            if not hasattr(local, "client"):
                local.client = self._create_vertex_client()
            return local.client

        def _process_batch(batch):
            return self._embed_vertex_batch_with_retry(_get_client(), batch)

        results = [None] * len(batches)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_process_batch, batch): idx
                for idx, (_, batch) in enumerate(batches)
            }
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()

        all_embeddings = []
        for r in results:
            all_embeddings.extend(r)
        return all_embeddings

    def _get_cache_key(self, text: str) -> str:
        """Generate cache key for text."""
        if self.provider == "local":
            model_id = self.settings.local_embedding_model
        elif self.provider == "vertex":
            model_id = self.settings.vertex_embedding_model
        else:
            model_id = self.settings.embedding_model
        return hashlib.md5(f"{model_id}:{text}".encode()).hexdigest()

    def _get_cached_embedding(self, text: str) -> list[float] | None:
        """Get embedding from cache if exists."""
        cache_key = self._get_cache_key(text)
        cache_file = self.cache_dir / f"{cache_key}.json"

        if cache_file.exists():
            try:
                with open(cache_file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return None

    def _cache_embedding(self, text: str, embedding: list[float]) -> None:
        """Cache an embedding."""
        cache_key = self._get_cache_key(text)
        cache_file = self.cache_dir / f"{cache_key}.json"

        try:
            with open(cache_file, "w") as f:
                json.dump(embedding, f)
        except IOError as e:
            logger.warning(f"Failed to cache embedding: {e}")

    def _generate_local_embedding(self, text: str) -> list[float]:
        """Generate embedding using local sentence-transformers model."""
        # Truncate if too long (most models have 512 token limit)
        max_chars = 2000  # Conservative limit for multilingual models
        if len(text) > max_chars:
            text = text[:max_chars]

        embedding = self.local_model.encode(text, convert_to_numpy=True)
        return embedding.tolist()

    def _generate_openai_embedding(self, text: str) -> list[float]:
        """Generate embedding using OpenAI API."""
        # Truncate if too long (8191 tokens max for OpenAI)
        max_chars = 8191 * 4  # Rough estimate: 4 chars per token for French
        if len(text) > max_chars:
            text = text[:max_chars]

        response = self.openai_client.embeddings.create(
            model=self.settings.embedding_model,
            input=text,
            dimensions=self.settings.embedding_dimensions,
        )
        return response.data[0].embedding

    def generate_embedding(self, text: str, use_cache: bool = True) -> list[float]:
        """
        Generate embedding for a single text.

        Args:
            text: Text to embed
            use_cache: Whether to use caching

        Returns:
            Embedding vector as list of floats
        """
        # Check cache
        if use_cache:
            cached = self._get_cached_embedding(text)
            if cached is not None:
                return cached

        # Generate embedding based on provider
        if self.provider == "local":
            embedding = self._generate_local_embedding(text)
        elif self.provider == "vertex":
            embedding = self._generate_vertex_embedding(text)
        else:
            embedding = self._generate_openai_embedding(text)

        # Cache the result
        if use_cache:
            self._cache_embedding(text, embedding)

        return embedding

    def generate_embeddings_batch(
        self, texts: list[str], batch_size: int = 32, use_cache: bool = True
    ) -> list[list[float]]:
        """
        Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed
            batch_size: Number of texts per batch
            use_cache: Whether to use caching

        Returns:
            List of embedding vectors
        """
        embeddings = []
        uncached_indices = []
        uncached_texts = []

        # Check cache first
        for i, text in enumerate(texts):
            if use_cache:
                cached = self._get_cached_embedding(text)
                if cached is not None:
                    embeddings.append(cached)
                    continue

            embeddings.append(None)  # Placeholder
            uncached_indices.append(i)
            uncached_texts.append(text)

        # Generate embeddings for uncached texts
        if uncached_texts:
            logger.info(f"Generating embeddings for {len(uncached_texts)} texts using {self.provider} provider")

            if self.provider == "local":
                # Local model can batch efficiently
                for i in range(0, len(uncached_texts), batch_size):
                    batch = uncached_texts[i : i + batch_size]
                    batch_indices = uncached_indices[i : i + batch_size]

                    # Truncate texts
                    max_chars = 2000
                    batch = [t[:max_chars] if len(t) > max_chars else t for t in batch]

                    # Batch encode
                    batch_embeddings = self.local_model.encode(
                        batch, convert_to_numpy=True, show_progress_bar=False
                    )

                    for j, embedding in enumerate(batch_embeddings):
                        idx = batch_indices[j]
                        emb_list = embedding.tolist()
                        embeddings[idx] = emb_list

                        if use_cache:
                            self._cache_embedding(uncached_texts[i + j], emb_list)
            elif self.provider == "vertex":
                # Vertex AI batch API - limit to 25 per request to stay under 20K token limit
                vertex_results = self._generate_vertex_embeddings_batch(
                    uncached_texts, batch_size=25
                )
                for j, emb_list in enumerate(vertex_results):
                    idx = uncached_indices[j]
                    embeddings[idx] = emb_list

                    if use_cache:
                        self._cache_embedding(uncached_texts[j], emb_list)
            else:
                # OpenAI batch API
                for i in range(0, len(uncached_texts), batch_size):
                    batch = uncached_texts[i : i + batch_size]
                    batch_indices = uncached_indices[i : i + batch_size]

                    # Truncate texts
                    max_chars = 8191 * 4
                    batch = [t[:max_chars] if len(t) > max_chars else t for t in batch]

                    response = self.openai_client.embeddings.create(
                        model=self.settings.embedding_model,
                        input=batch,
                        dimensions=self.settings.embedding_dimensions,
                    )

                    for j, emb_data in enumerate(response.data):
                        idx = batch_indices[j]
                        embedding = emb_data.embedding
                        embeddings[idx] = embedding

                        if use_cache:
                            self._cache_embedding(uncached_texts[i + j], embedding)

        return embeddings

    def embed_chunks(
        self, chunks: list[Chunk], batch_size: int = 32
    ) -> list[tuple[Chunk, list[float]]]:
        """
        Generate embeddings for a list of chunks.

        Args:
            chunks: List of Chunk objects
            batch_size: Batch size for processing

        Returns:
            List of (chunk, embedding) tuples
        """
        texts = [chunk.content for chunk in chunks]
        embeddings = self.generate_embeddings_batch(texts, batch_size)

        return list(zip(chunks, embeddings))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """
    Calculate cosine similarity between two vectors.

    Args:
        a: First vector
        b: Second vector

    Returns:
        Cosine similarity (0 to 1)
    """
    a = np.array(a)
    b = np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def find_similar_chunks(
    query_embedding: list[float],
    chunk_embeddings: list[tuple[Chunk, list[float]]],
    top_k: int = 10,
    min_similarity: float = 0.5,
) -> list[tuple[Chunk, float]]:
    """
    Find most similar chunks to a query.

    Args:
        query_embedding: Query vector
        chunk_embeddings: List of (chunk, embedding) tuples
        top_k: Number of results to return
        min_similarity: Minimum similarity threshold

    Returns:
        List of (chunk, similarity) tuples, sorted by similarity
    """
    results = []

    for chunk, embedding in chunk_embeddings:
        similarity = cosine_similarity(query_embedding, embedding)
        if similarity >= min_similarity:
            results.append((chunk, similarity))

    # Sort by similarity descending
    results.sort(key=lambda x: x[1], reverse=True)

    return results[:top_k]


class VectorStore:
    """Simple in-memory vector store for development."""

    def __init__(self):
        self.chunks: list[Chunk] = []
        self.embeddings: list[list[float]] = []

    def add(self, chunk: Chunk, embedding: list[float]) -> int:
        """Add a chunk and its embedding. Returns the index."""
        if isinstance(self.embeddings, np.ndarray):
            self.embeddings = self.embeddings.tolist()
        self.chunks.append(chunk)
        self.embeddings.append(embedding)
        return len(self.chunks) - 1

    def add_batch(self, chunk_embeddings: list[tuple[Chunk, list[float]]]) -> None:
        """Add multiple chunks and embeddings at once (avoids per-item isinstance check)."""
        if isinstance(self.embeddings, np.ndarray):
            self.embeddings = self.embeddings.tolist()
        for chunk, embedding in chunk_embeddings:
            self.chunks.append(chunk)
            self.embeddings.append(embedding)

    def search(
        self, query_embedding: list[float], top_k: int = 10, min_similarity: float = 0.5
    ) -> list[tuple[Chunk, float]]:
        """
        Search for similar chunks using vectorized cosine similarity.

        Args:
            query_embedding: Query vector
            top_k: Number of results
            min_similarity: Minimum similarity threshold

        Returns:
            List of (chunk, similarity) tuples
        """
        if len(self.chunks) == 0:
            return []

        if isinstance(self.embeddings, np.ndarray):
            emb_matrix = self.embeddings
        else:
            emb_matrix = np.array(self.embeddings, dtype=np.float32)

        query_arr = np.array(query_embedding, dtype=np.float32)

        # Vectorized cosine similarity in one matrix op
        norms = np.linalg.norm(emb_matrix, axis=1)
        query_norm = np.linalg.norm(query_arr)
        similarities = emb_matrix @ query_arr / (norms * query_norm + 1e-10)

        # Filter by threshold + top-k
        mask = similarities >= min_similarity
        indices = np.where(mask)[0]
        if len(indices) == 0:
            return []

        top_indices = indices[np.argsort(-similarities[indices])[:top_k]]
        return [(self.chunks[i], float(similarities[i])) for i in top_indices]

    def save(self, path: Path) -> None:
        """Save vector store to disk using numpy binary format.

        Creates two files:
        - {path}.npz: Compressed numpy array of embeddings (float32)
        - {path}.meta.json: Lightweight JSON with chunk metadata only
        """
        # Normalize: strip .json suffix if present
        path = Path(str(path).removesuffix(".json"))
        path.parent.mkdir(parents=True, exist_ok=True)

        # Embeddings → compressed numpy float32
        if isinstance(self.embeddings, np.ndarray):
            embeddings_array = self.embeddings.astype(np.float32)
        else:
            embeddings_array = np.array(self.embeddings, dtype=np.float32)
        npz_path = path.with_suffix(".npz")
        np.savez_compressed(npz_path, embeddings=embeddings_array)

        # Chunk metadata → lightweight JSON (no embeddings)
        chunks_data = [
            {
                "content": c.content,
                "metadata": c.metadata,
                "chunk_type": c.chunk_type,
                "source_id": c.source_id,
                "position": c.position,
            }
            for c in self.chunks
        ]
        meta_path = path.with_suffix(".meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"chunks": chunks_data}, f, ensure_ascii=False)

        logger.info(f"Saved {len(self.chunks)} chunks to {npz_path} + {meta_path}")

    def load(self, path: Path) -> None:
        """Load vector store from disk.

        Auto-detects format:
        - New format: .npz (embeddings) + .meta.json (chunk metadata)
        - Legacy format: single .json file with everything
        """
        # Normalize path
        path_str = str(path)
        base_path = Path(path_str.removesuffix(".json"))
        npz_path = base_path.with_suffix(".npz")
        meta_path = base_path.with_suffix(".meta.json")
        json_path = base_path.with_suffix(".json")

        if npz_path.exists() and meta_path.exists():
            # New binary format
            logger.info(f"Loading binary format from {npz_path}")
            with np.load(npz_path) as data:
                self.embeddings = data["embeddings"].astype(np.float32)

            with open(meta_path, encoding="utf-8") as f:
                metadata = json.load(f)

            self.chunks = [
                Chunk(
                    content=c["content"],
                    metadata=c["metadata"],
                    chunk_type=c["chunk_type"],
                    source_id=c["source_id"],
                    position=c["position"],
                )
                for c in metadata["chunks"]
            ]
        elif json_path.exists():
            # Legacy JSON format
            logger.warning(f"Loading legacy JSON format from {json_path}")
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)

            self.chunks = [
                Chunk(
                    content=c["content"],
                    metadata=c["metadata"],
                    chunk_type=c["chunk_type"],
                    source_id=c["source_id"],
                    position=c["position"],
                )
                for c in data["chunks"]
            ]
            self.embeddings = data["embeddings"]
        else:
            raise FileNotFoundError(
                f"Vector store not found at {base_path} "
                f"(looked for {npz_path}, {meta_path}, {json_path})"
            )

        # Validate chunk/embedding count match
        n_chunks = len(self.chunks)
        n_embeddings = self.embeddings.shape[0] if isinstance(self.embeddings, np.ndarray) else len(self.embeddings)
        if n_chunks != n_embeddings:
            n = min(n_chunks, n_embeddings)
            logger.warning(
                f"Chunk/embedding count mismatch: {n_chunks} chunks vs {n_embeddings} embeddings. "
                f"Truncating to {n} entries."
            )
            self.chunks = self.chunks[:n]
            if isinstance(self.embeddings, np.ndarray):
                self.embeddings = self.embeddings[:n]
            else:
                self.embeddings = self.embeddings[:n]

        logger.info(f"Loaded {len(self.chunks)} chunks from {base_path}")

    def count(self, chunk_type: str | None = None) -> int:
        """Count chunks, optionally filtered by type."""
        if chunk_type is None:
            return len(self.chunks)
        return sum(1 for c in self.chunks if c.chunk_type == chunk_type)

    def delete_by_type(self, chunk_type: str) -> int:
        """Delete all chunks of a given type."""
        original_count = len(self.chunks)
        indices_to_keep = [i for i, c in enumerate(self.chunks) if c.chunk_type != chunk_type]
        self.chunks = [self.chunks[i] for i in indices_to_keep]
        if isinstance(self.embeddings, np.ndarray):
            self.embeddings = self.embeddings[indices_to_keep]
        else:
            self.embeddings = [self.embeddings[i] for i in indices_to_keep]
        deleted = original_count - len(self.chunks)
        logger.info(f"Deleted {deleted} chunks of type '{chunk_type}'")
        return deleted

    def close(self):
        """No-op for in-memory store."""
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __len__(self) -> int:
        return len(self.chunks)
