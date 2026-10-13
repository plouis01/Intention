#!/usr/bin/env python3
"""
Test pgvector performance on local or Railway database.

Usage:
    export $(grep -v '^#' .env.local | xargs)  # For local testing
    python scripts/test_pgvector_performance.py

    export $(grep -v '^#' .env | xargs)  # For Railway testing
    python scripts/test_pgvector_performance.py
"""

import os
import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.indexing.vector_stores import create_vector_store
from src.indexing.embeddings import EmbeddingService
from src.utils.config import get_settings


def main():
    """Run performance benchmark on pgvector store."""
    print("=" * 70)
    print("pgvector Performance Benchmark")
    print("=" * 70)
    print()

    # Get settings
    settings = get_settings()

    # Display configuration
    print(f"Configuration:")
    print(f"  Database:     {settings.database_url[:50]}...")
    print(f"  Vector store: {settings.vector_store_provider}")
    print(f"  Embedding:    {settings.local_embedding_model if settings.embedding_provider == 'local' else settings.embedding_model}")
    print()

    # Initialize services
    print("Initializing services...")
    embedding_service = EmbeddingService()

    # Create vector store
    if settings.vector_store_provider != "pgvector":
        print(f"\n⚠️  WARNING: VECTOR_STORE_PROVIDER is '{settings.vector_store_provider}', not 'pgvector'")
        print("   Set VECTOR_STORE_PROVIDER=pgvector to test pgvector performance")
        print()
        response = input("Continue anyway? (y/N): ")
        if response.lower() != 'y':
            print("Aborted.")
            sys.exit(0)

    store = create_vector_store(
        settings.vector_store_provider,
        embedding_dim=768,
    )

    print("✓ Services initialized")
    print()

    # Test queries
    test_queries = [
        "plus-value cession titre holding",
        "imposition bénéfices entreprise",
        "crédit impôt recherche innovation",
        "fiscalité immobilière résidence principale",
        "exonération transmission entreprise familiale",
    ]

    print(f"Running benchmark with {len(test_queries)} different queries...")
    print(f"Each query will be executed 3 times (warm-up + 2 measurements)")
    print()

    # Statistics
    all_times = []
    all_results_counts = []

    for i, query_text in enumerate(test_queries, 1):
        print(f"Query {i}/{len(test_queries)}: \"{query_text}\"")

        # Generate embedding
        embed_start = time.time()
        query_embedding = embedding_service.generate_embedding(query_text)
        embed_time = (time.time() - embed_start) * 1000

        print(f"  Embedding generation: {embed_time:.2f}ms")

        # Run searches (warm-up + measurements)
        query_times = []
        results_count = 0

        for run in range(3):
            start = time.time()
            results = store.search(
                query_embedding,
                top_k=10,
                min_similarity=0.5,
                chunk_type="intervention"
            )
            elapsed = (time.time() - start) * 1000  # ms

            if run == 0:
                # Warm-up run (might be slower due to cache)
                print(f"  Warm-up:  {elapsed:.2f}ms ({len(results)} results)")
                results_count = len(results)
            else:
                # Actual measurements
                query_times.append(elapsed)
                print(f"  Run {run}:    {elapsed:.2f}ms ({len(results)} results)")

        # Statistics for this query
        avg_time = sum(query_times) / len(query_times)
        all_times.extend(query_times)
        all_results_counts.append(results_count)

        print(f"  Average (excl. warm-up): {avg_time:.2f}ms")
        print()

    # Overall statistics
    print("=" * 70)
    print("Overall Performance Summary")
    print("=" * 70)
    print()

    avg_time = sum(all_times) / len(all_times)
    min_time = min(all_times)
    max_time = max(all_times)
    median_time = sorted(all_times)[len(all_times) // 2]
    avg_results = sum(all_results_counts) / len(all_results_counts)

    print(f"Search Performance:")
    print(f"  Average:   {avg_time:.2f}ms")
    print(f"  Median:    {median_time:.2f}ms")
    print(f"  Min:       {min_time:.2f}ms")
    print(f"  Max:       {max_time:.2f}ms")
    print()

    print(f"Results:")
    print(f"  Average results per query: {avg_results:.1f}")
    print()

    # Performance verdict
    print("Performance Verdict:")
    if avg_time < 50:
        print(f"  ✓ EXCELLENT: pgvector is highly optimized ({avg_time:.0f}ms avg)")
        print(f"    Perfect for production use with sub-50ms latency")
    elif avg_time < 100:
        print(f"  ✓ GOOD: pgvector performance is acceptable ({avg_time:.0f}ms avg)")
        print(f"    Suitable for production, consider tuning ef_search if needed")
    elif avg_time < 200:
        print(f"  ⚠ ACCEPTABLE: Performance is okay but could be better ({avg_time:.0f}ms avg)")
        print(f"    Consider reindexing or checking HNSW parameters")
    else:
        print(f"  ✗ SLOW: Performance needs improvement ({avg_time:.0f}ms avg)")
        print(f"    Check if indexes are being used (EXPLAIN ANALYZE)")
        print(f"    Consider rebuilding indexes with higher ef_construction")
    print()

    # Recommendations
    print("Recommendations:")
    if avg_time < 50:
        print("  • Performance is optimal, no action needed")
        print("  • Current configuration is production-ready")
    elif avg_time < 100:
        print("  • Try increasing ef_search for better recall:")
        print("    SET hnsw.ef_search = 100 (default: 40)")
        print("  • Consider reindexing if performance degrades over time")
    else:
        print("  • Verify indexes are being used:")
        print("    psql $DATABASE_URL -c 'EXPLAIN ANALYZE SELECT ...'")
        print("  • Rebuild indexes if they're fragmented:")
        print("    psql $DATABASE_URL -f scripts/reindex_new_chunks.sql")
        print("  • Check PostgreSQL configuration:")
        print("    SHOW maintenance_work_mem;  (should be >= 1GB)")

    print()
    print("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nBenchmark interrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n✗ Error during benchmark: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
