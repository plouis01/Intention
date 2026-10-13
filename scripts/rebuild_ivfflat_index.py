"""Rebuild IVFFlat index with optimized parameters - Story 4.1.

Recalibrates the IVFFlat index on chunk_embeddings.embedding_vector:
- lists = sqrt(row_count) - calculated dynamically based on actual row count
- probes = 40 (set at query time in vector_stores.py)

This improves recall from ~90% to ~97-98%.

Usage:
    python scripts/rebuild_ivfflat_index.py           # Rebuild index
    python scripts/rebuild_ivfflat_index.py --check   # Check current index config
"""

import argparse
import math
import sys
import time

from sqlalchemy import create_engine, text

from src.utils.config import get_settings


def calculate_optimal_lists(row_count: int) -> int:
    """
    Calculate optimal IVFFlat lists parameter.

    Formula: lists = sqrt(row_count)
    This provides good balance between index size and search quality.

    Args:
        row_count: Number of rows with embeddings

    Returns:
        Optimal lists parameter (integer)
    """
    return int(math.sqrt(row_count))


def get_pg_engine():
    """Get PostgreSQL engine from settings."""
    settings = get_settings()
    url = settings.effective_database_url
    if not url.startswith("postgresql"):
        print(f"ERROR: Not a PostgreSQL URL: {url[:30]}...")
        sys.exit(1)
    return create_engine(url)


def check_index():
    """Check current IVFFlat index configuration."""
    engine = get_pg_engine()
    with engine.connect() as conn:
        # Count rows
        result = conn.execute(text(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE embedding_vector IS NOT NULL"
        ))
        row_count = result.scalar()
        print(f"Rows with embedding_vector: {row_count:,}")
        print(f"Optimal lists (sqrt): {int(row_count**0.5)}")

        # Check existing indexes
        result = conn.execute(text("""
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE tablename = 'chunk_embeddings'
            AND indexdef LIKE '%ivfflat%'
        """))
        indexes = result.fetchall()
        if indexes:
            print("\nExisting IVFFlat indexes:")
            for name, defn in indexes:
                print(f"  {name}: {defn}")
        else:
            print("\nNo IVFFlat indexes found")

        # Check if HNSW exists
        result = conn.execute(text("""
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE tablename = 'chunk_embeddings'
            AND indexdef LIKE '%hnsw%'
        """))
        hnsw_indexes = result.fetchall()
        if hnsw_indexes:
            print("\nExisting HNSW indexes:")
            for name, defn in hnsw_indexes:
                print(f"  {name}: {defn}")


def rebuild_index():
    """Rebuild IVFFlat index with optimized parameters."""
    engine = get_pg_engine()
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")

        # Count rows first
        result = conn.execute(text(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE embedding_vector IS NOT NULL"
        ))
        row_count = result.scalar()
        print(f"Rows with embedding_vector: {row_count:,}")

        if row_count == 0:
            print("No rows to index. Aborting.")
            return

        # Calculate optimal lists parameter
        optimal_lists = calculate_optimal_lists(row_count)
        print(f"Calculated optimal lists: {optimal_lists} (sqrt of {row_count:,})")

        # Increase maintenance_work_mem for this session to allow large index builds
        # IVFFlat with 1722 lists requires ~712 MB; set 1 GB to be safe
        print("\nSetting maintenance_work_mem = 1GB for this session...")
        conn.execute(text("SET maintenance_work_mem = '1GB'"))

        # Drop existing IVFFlat index if exists
        print("Dropping existing IVFFlat index (if any)...")
        conn.execute(text(
            "DROP INDEX IF EXISTS ix_chunk_embeddings_ivfflat"
        ))
        conn.execute(text(
            "DROP INDEX IF EXISTS idx_chunk_embeddings_vector"
        ))

        # Build new index
        print(f"Building IVFFlat index with lists={optimal_lists}...")
        print("This may take several minutes...")
        start = time.time()

        conn.execute(text(f"""
            CREATE INDEX ix_chunk_embeddings_ivfflat
            ON chunk_embeddings
            USING ivfflat (embedding_vector vector_cosine_ops)
            WITH (lists = {optimal_lists})
        """))

        elapsed = time.time() - start
        print(f"Index built in {elapsed:.1f}s")

        # Verify
        result = conn.execute(text("""
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE tablename = 'chunk_embeddings'
            AND indexname = 'ix_chunk_embeddings_ivfflat'
        """))
        idx = result.fetchone()
        if idx:
            print(f"\nIndex created: {idx[1]}")
        else:
            print("\nWARNING: Index creation may have failed")

        # Analyze table for query planner
        print("Running ANALYZE...")
        conn.execute(text("ANALYZE chunk_embeddings"))
        print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rebuild IVFFlat index")
    parser.add_argument("--check", action="store_true", help="Check current index config")
    args = parser.parse_args()

    if args.check:
        check_index()
    else:
        rebuild_index()
