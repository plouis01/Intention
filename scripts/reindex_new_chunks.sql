-- ============================================================================
-- Incremental reindexing for chunk_embeddings HNSW indexes
-- ============================================================================
-- HNSW indexes are updated automatically on INSERT/UPDATE, so manual
-- reindexing is only needed for optimization after bulk operations.
--
-- When to run:
-- - After bulk imports (100,000+ new chunks)
-- - If query performance degrades over time
-- - After large DELETE operations
-- - Weekly/monthly maintenance
--
-- Note: REINDEX CONCURRENTLY does NOT block reads or writes.
-- ============================================================================

\echo '===================================================================='
\echo 'pgvector HNSW Index Maintenance'
\echo '===================================================================='
\echo ''

-- Step 1: Analyze current index health
-- ============================================================================
\echo 'Step 1: Checking index health and row counts...'
\echo '--------------------------------------------------------------------'

DO $$
DECLARE
    total_rows BIGINT;
    intervention_rows BIGINT;
    amendement_rows BIGINT;
    vector_rows BIGINT;
BEGIN
    SELECT COUNT(*) INTO total_rows FROM chunk_embeddings;
    SELECT COUNT(*) INTO intervention_rows FROM chunk_embeddings WHERE chunk_type = 'intervention';
    SELECT COUNT(*) INTO amendement_rows FROM chunk_embeddings WHERE chunk_type = 'amendement';
    SELECT COUNT(*) INTO vector_rows FROM chunk_embeddings WHERE embedding_vector IS NOT NULL;

    RAISE NOTICE 'Current database state:';
    RAISE NOTICE '  Total chunks: %', total_rows;
    RAISE NOTICE '  Interventions: %', intervention_rows;
    RAISE NOTICE '  Amendements: %', amendement_rows;
    RAISE NOTICE '  With embedding_vector: %', vector_rows;
END $$;

-- Show index sizes
SELECT
    indexname AS "Index",
    pg_size_pretty(pg_relation_size(indexname::regclass)) AS "Size",
    CASE WHEN indisvalid THEN 'Valid' ELSE 'INVALID' END AS "Status"
FROM pg_indexes
JOIN pg_index ON pg_indexes.indexname = pg_index.indexrelid::regclass::text
WHERE tablename = 'chunk_embeddings'
  AND indexdef LIKE '%hnsw%'
ORDER BY indexname;

\echo ''


-- Step 2: Check if reindexing is needed
-- ============================================================================
\echo 'Step 2: Determining if reindexing is beneficial...'
\echo '--------------------------------------------------------------------'

DO $$
DECLARE
    last_vac TIMESTAMP;
    needs_reindex BOOLEAN := FALSE;
BEGIN
    SELECT last_vacuum INTO last_vac
    FROM pg_stat_user_tables
    WHERE relname = 'chunk_embeddings';

    RAISE NOTICE 'Last vacuum: %', COALESCE(last_vac::text, 'Never');

    IF last_vac IS NULL OR last_vac < NOW() - INTERVAL '7 days' THEN
        needs_reindex := TRUE;
        RAISE NOTICE 'Index may benefit from rebuild (last vacuumed > 7 days ago)';
    ELSE
        RAISE NOTICE 'Recent vacuum detected, indexes likely healthy';
    END IF;

    PERFORM set_config('reindex.needed', needs_reindex::text, false);
END $$;
\echo ''


-- Step 3: Sync any un-migrated embeddings
-- ============================================================================
\echo 'Step 3: Syncing un-migrated embeddings...'
\echo '--------------------------------------------------------------------'

DO $$
DECLARE
    synced BIGINT;
BEGIN
    UPDATE chunk_embeddings
    SET embedding_vector = embedding::text::vector(384)
    WHERE embedding_vector IS NULL
      AND embedding IS NOT NULL
      AND jsonb_array_length(embedding::jsonb) = 384;

    GET DIAGNOSTICS synced = ROW_COUNT;

    IF synced > 0 THEN
        RAISE NOTICE 'Synced % rows from embedding to embedding_vector', synced;
    ELSE
        RAISE NOTICE 'All embeddings already synced';
    END IF;
END $$;
\echo ''


-- Step 4: Rebuild indexes (CONCURRENTLY - no write locks)
-- ============================================================================
-- REINDEX CONCURRENTLY cannot be inside a DO block or transaction.
-- We run them as top-level statements unconditionally
-- (they are fast no-ops if the index is already optimal).

\echo 'Step 4: Rebuilding HNSW indexes (CONCURRENTLY)...'
\echo '--------------------------------------------------------------------'
\echo 'This may take 60-120 minutes. Reads and writes continue normally.'
\echo 'Monitor: SELECT * FROM pg_stat_progress_create_index;'
\echo ''

SET maintenance_work_mem = '2GB';
SET max_parallel_maintenance_workers = 4;

REINDEX INDEX CONCURRENTLY idx_embeddings_intervention_vector_hnsw;
REINDEX INDEX CONCURRENTLY idx_embeddings_amendement_vector_hnsw;

\echo 'Indexes rebuilt.'
\echo ''


-- Step 5: Vacuum and analyze
-- ============================================================================
\echo 'Step 5: Optimizing table statistics...'
\echo '--------------------------------------------------------------------'

VACUUM (VERBOSE, ANALYZE) chunk_embeddings;

\echo 'Table vacuumed and analyzed'
\echo ''


-- Step 6: Verify index health
-- ============================================================================
\echo 'Step 6: Post-reindex verification...'
\echo '--------------------------------------------------------------------'

SELECT
    indexname AS "Index",
    CASE
        WHEN indisvalid THEN 'Valid and ready'
        ELSE 'INVALID (rebuild failed?)'
    END AS "Status",
    pg_size_pretty(pg_relation_size(indexname::regclass)) AS "Size"
FROM pg_indexes
JOIN pg_index ON pg_indexes.indexname = pg_index.indexrelid::regclass::text
WHERE tablename = 'chunk_embeddings'
  AND indexdef LIKE '%hnsw%'
ORDER BY indexname;

\echo ''


-- Step 7: Test query performance
-- ============================================================================
\echo '===================================================================='
\echo 'Test Query Performance'
\echo '===================================================================='
\echo ''

\timing on

EXPLAIN (ANALYZE, BUFFERS) SELECT
    content,
    1 - (embedding_vector <=> (
        SELECT embedding_vector FROM chunk_embeddings
        WHERE chunk_type = 'intervention' AND embedding_vector IS NOT NULL
        LIMIT 1
    )) AS similarity
FROM chunk_embeddings
WHERE chunk_type = 'intervention'
  AND embedding_vector IS NOT NULL
ORDER BY embedding_vector <=> (
    SELECT embedding_vector FROM chunk_embeddings
    WHERE chunk_type = 'intervention' AND embedding_vector IS NOT NULL
    LIMIT 1
)
LIMIT 10;

\timing off

\echo ''
\echo 'Look for "Index Scan using idx_embeddings_intervention_vector_hnsw" above'
\echo 'Execution time should be < 100ms'
\echo ''

\echo '===================================================================='
\echo 'Reindexing complete!'
\echo '===================================================================='
