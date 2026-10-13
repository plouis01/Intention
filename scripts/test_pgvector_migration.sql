-- ============================================================================
-- TEST pgvector migration on Railway (10k chunks, fully reversible)
-- ============================================================================
-- This script:
-- 1. Adds embedding_vector column
-- 2. Migrates 10,000 rows
-- 3. Creates a TEMPORARY (non-HNSW) index for quick testing
-- 4. Runs test queries and shows results
-- 5. REVERTS everything at the end
--
-- Safe: no trigger created, pipeline unaffected, full rollback at end.
-- Duration: ~2-3 minutes total
--
-- Usage:
--   psql $DATABASE_URL -f scripts/test_pgvector_migration.sql
-- ============================================================================

\echo ''
\echo '===================================================================='
\echo '  pgvector Migration TEST (10k chunks, full revert at end)'
\echo '===================================================================='
\echo ''

\timing on

-- ============================================================================
-- Step 1: Baseline - current state
-- ============================================================================
\echo '--- Step 1: Baseline ---'

SELECT
    COUNT(*) AS total_rows,
    COUNT(*) FILTER (WHERE chunk_type = 'intervention') AS interventions,
    COUNT(*) FILTER (WHERE chunk_type = 'amendement') AS amendements,
    COUNT(DISTINCT embedding_model) AS embedding_models,
    MIN(jsonb_array_length(embedding::jsonb)) AS min_dim,
    MAX(jsonb_array_length(embedding::jsonb)) AS max_dim
FROM chunk_embeddings
WHERE embedding IS NOT NULL;

\echo ''

-- ============================================================================
-- Step 2: Enable pgvector extension
-- ============================================================================
\echo '--- Step 2: Enable pgvector extension ---'

CREATE EXTENSION IF NOT EXISTS vector;
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';

\echo ''

-- ============================================================================
-- Step 3: Add embedding_vector column (if not exists)
-- ============================================================================
\echo '--- Step 3: Add embedding_vector column ---'

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'chunk_embeddings'
          AND column_name = 'embedding_vector'
    ) THEN
        ALTER TABLE chunk_embeddings ADD COLUMN embedding_vector vector(384);
        RAISE NOTICE 'Added embedding_vector column';
    ELSE
        RAISE NOTICE 'embedding_vector column already exists';
    END IF;
END $$;

\echo ''

-- ============================================================================
-- Step 4: Dimension check (all embeddings must be 384)
-- ============================================================================
\echo '--- Step 4: Dimension validation ---'

SELECT
    jsonb_array_length(embedding::jsonb) AS dimension,
    COUNT(*) AS row_count
FROM chunk_embeddings
WHERE embedding IS NOT NULL
GROUP BY jsonb_array_length(embedding::jsonb)
ORDER BY row_count DESC;

\echo ''

-- ============================================================================
-- Step 5: Migrate 10,000 rows
-- ============================================================================
\echo '--- Step 5: Migrate 10,000 rows ---'

UPDATE chunk_embeddings
SET embedding_vector = embedding::text::vector(384)
WHERE id IN (
    SELECT id FROM chunk_embeddings
    WHERE embedding_vector IS NULL
      AND embedding IS NOT NULL
      AND jsonb_array_length(embedding::jsonb) = 384
    LIMIT 10000
);

\echo ''

-- Verify migration
SELECT
    COUNT(*) FILTER (WHERE embedding_vector IS NOT NULL) AS migrated,
    COUNT(*) FILTER (WHERE embedding_vector IS NULL AND embedding IS NOT NULL) AS not_migrated,
    COUNT(*) AS total
FROM chunk_embeddings;

\echo ''

-- ============================================================================
-- Step 6: Verify data integrity (spot check)
-- ============================================================================
\echo '--- Step 6: Data integrity check ---'

-- Compare first 5 elements of JSON vs vector for 5 random rows
SELECT
    id,
    (embedding->0)::text AS json_dim0,
    embedding_vector[1]::text AS vec_dim0,
    ABS((embedding->0)::float - embedding_vector[1]::float) < 0.0001 AS dim0_match,
    (embedding->383)::text AS json_dim383,
    embedding_vector[384]::text AS vec_dim383,
    ABS((embedding->383)::float - embedding_vector[384]::float) < 0.0001 AS dim383_match
FROM chunk_embeddings
WHERE embedding_vector IS NOT NULL
ORDER BY random()
LIMIT 5;

\echo ''
\echo 'dim0_match and dim383_match should both be true for all rows.'
\echo ''

-- ============================================================================
-- Step 7: Test similarity search WITHOUT index (brute force)
-- ============================================================================
\echo '--- Step 7: Similarity search test (brute force, no HNSW) ---'

-- Use a real embedding from the table as query vector
EXPLAIN (ANALYZE, COSTS, BUFFERS)
SELECT
    id, chunk_type, LEFT(content, 80) AS content_preview,
    1 - (embedding_vector <=> (
        SELECT embedding_vector FROM chunk_embeddings
        WHERE embedding_vector IS NOT NULL AND chunk_type = 'intervention'
        LIMIT 1
    )) AS similarity
FROM chunk_embeddings
WHERE embedding_vector IS NOT NULL
  AND chunk_type = 'intervention'
ORDER BY embedding_vector <=> (
    SELECT embedding_vector FROM chunk_embeddings
    WHERE embedding_vector IS NOT NULL AND chunk_type = 'intervention'
    LIMIT 1
)
LIMIT 10;

\echo ''

-- ============================================================================
-- Step 8: Create a quick test index (NOT CONCURRENTLY for speed, on 10k rows only)
-- ============================================================================
\echo '--- Step 8: Create test HNSW index on migrated rows ---'

-- Partial index on rows that have embedding_vector (only our 10k rows)
CREATE INDEX IF NOT EXISTS idx_test_pgvector_hnsw
ON chunk_embeddings
USING hnsw (embedding_vector vector_cosine_ops)
WHERE embedding_vector IS NOT NULL;

\echo ''

-- ============================================================================
-- Step 9: Test similarity search WITH HNSW index
-- ============================================================================
\echo '--- Step 9: Similarity search with HNSW index ---'

-- Force index usage
SET enable_seqscan = off;

EXPLAIN (ANALYZE, COSTS, BUFFERS)
SELECT
    id, chunk_type, LEFT(content, 80) AS content_preview,
    1 - (embedding_vector <=> (
        SELECT embedding_vector FROM chunk_embeddings
        WHERE embedding_vector IS NOT NULL AND chunk_type = 'intervention'
        LIMIT 1
    )) AS similarity
FROM chunk_embeddings
WHERE embedding_vector IS NOT NULL
  AND chunk_type = 'intervention'
ORDER BY embedding_vector <=> (
    SELECT embedding_vector FROM chunk_embeddings
    WHERE embedding_vector IS NOT NULL AND chunk_type = 'intervention'
    LIMIT 1
)
LIMIT 10;

SET enable_seqscan = on;

\echo ''

-- ============================================================================
-- Step 10: Show actual search results (top 10 most similar)
-- ============================================================================
\echo '--- Step 10: Top 10 search results ---'

SELECT
    id,
    chunk_type,
    LEFT(content, 120) AS content_preview,
    ROUND((1 - (embedding_vector <=> (
        SELECT embedding_vector FROM chunk_embeddings
        WHERE embedding_vector IS NOT NULL AND chunk_type = 'intervention'
        LIMIT 1
    )))::numeric, 4) AS similarity,
    chunk_metadata->>'orateur' AS orateur,
    chunk_metadata->>'date' AS date
FROM chunk_embeddings
WHERE embedding_vector IS NOT NULL
  AND chunk_type = 'intervention'
ORDER BY embedding_vector <=> (
    SELECT embedding_vector FROM chunk_embeddings
    WHERE embedding_vector IS NOT NULL AND chunk_type = 'intervention'
    LIMIT 1
)
LIMIT 10;

\echo ''

-- ============================================================================
-- Step 11: Storage impact
-- ============================================================================
\echo '--- Step 11: Storage impact ---'

SELECT
    pg_size_pretty(pg_total_relation_size('chunk_embeddings')) AS total_size,
    pg_size_pretty(pg_relation_size('chunk_embeddings')) AS table_size,
    pg_size_pretty(
        pg_total_relation_size('chunk_embeddings') - pg_relation_size('chunk_embeddings')
    ) AS indexes_size;

-- Index size specifically
SELECT
    indexname,
    pg_size_pretty(pg_relation_size(indexname::regclass)) AS size
FROM pg_indexes
WHERE tablename = 'chunk_embeddings'
  AND indexname = 'idx_test_pgvector_hnsw';

\echo ''

-- ============================================================================
-- Step 12: FULL REVERT
-- ============================================================================
\echo '===================================================================='
\echo '  REVERTING ALL CHANGES'
\echo '===================================================================='
\echo ''

-- Drop test index
DROP INDEX IF EXISTS idx_test_pgvector_hnsw;
\echo 'Dropped test HNSW index'

-- Drop embedding_vector column (removes all migrated data)
ALTER TABLE chunk_embeddings DROP COLUMN IF EXISTS embedding_vector;
\echo 'Dropped embedding_vector column'

-- Verify revert
SELECT
    column_name
FROM information_schema.columns
WHERE table_name = 'chunk_embeddings'
  AND column_name = 'embedding_vector';
-- Should return 0 rows

\echo ''
\echo 'Revert verification: if no rows above, embedding_vector is gone.'
\echo ''

-- Final state
SELECT
    COUNT(*) AS total_rows,
    pg_size_pretty(pg_total_relation_size('chunk_embeddings')) AS total_size
FROM chunk_embeddings;

\timing off

\echo ''
\echo '===================================================================='
\echo '  TEST COMPLETE - All changes reverted'
\echo '===================================================================='
\echo ''
\echo 'What to look for in the output above:'
\echo '  1. Step 4: All dimensions should be 384'
\echo '  2. Step 6: dim0_match and dim383_match should be true (data integrity)'
\echo '  3. Step 7 vs 9: HNSW index should be faster than brute force'
\echo '  4. Step 9: EXPLAIN should show "Index Scan using idx_test_pgvector_hnsw"'
\echo '  5. Step 10: Results should have similarity scores between 0 and 1'
\echo '  6. Step 12: embedding_vector column should be gone'
\echo ''
