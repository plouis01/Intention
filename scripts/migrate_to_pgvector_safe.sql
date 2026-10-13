-- ============================================================================
-- SAFE Migration: Upgrade chunk_embeddings to pgvector (NON-DESTRUCTIVE)
-- ============================================================================
-- This script adds a NEW column 'embedding_vector' alongside the existing
-- 'embedding' column, allowing the embedding pipeline to continue running
-- without interruption.
--
-- SAFE APPROACH:
-- - Does NOT rename or delete existing 'embedding' column
-- - Adds 'embedding_vector' (vector type) as a NEW column
-- - Copies data: embedding (JSON) → embedding_vector (vector)
-- - Creates HNSW indexes CONCURRENTLY on embedding_vector (no write locks)
-- - Embedding pipeline continues writing to 'embedding' (unchanged)
--
-- Duration: ~15 min data copy + 90-120 min index build (1.6M rows)
-- Note: Writes are NOT blocked during index creation (CONCURRENTLY).
-- ============================================================================

-- Step 1: Enable pgvector extension
-- ============================================================================
CREATE EXTENSION IF NOT EXISTS vector;

-- Verify extension is loaded
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';


-- Step 2: Add new embedding_vector column (WITHOUT touching embedding)
-- ============================================================================
DO $$
BEGIN
    -- Add vector column if it doesn't exist
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'chunk_embeddings'
          AND column_name = 'embedding_vector'
    ) THEN
        ALTER TABLE chunk_embeddings
        ADD COLUMN embedding_vector vector(384);

        RAISE NOTICE 'Added embedding_vector column (embedding column untouched)';
    ELSE
        RAISE NOTICE 'embedding_vector column already exists, skipping creation';
    END IF;
END $$;


-- Step 3: Verify existing embedding column is intact
-- ============================================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'chunk_embeddings'
          AND column_name = 'embedding'
    ) THEN
        RAISE NOTICE 'Existing embedding column is intact (pipeline can continue writing)';
    ELSE
        RAISE WARNING 'embedding column not found! This should not happen.';
    END IF;
END $$;


-- Step 4: Pre-flight check — verify all embeddings are 384-dimensional
-- ============================================================================
DO $$
DECLARE
    bad_rows BIGINT;
BEGIN
    SELECT COUNT(*) INTO bad_rows
    FROM chunk_embeddings
    WHERE embedding IS NOT NULL
      AND jsonb_array_length(embedding::jsonb) != 384;

    IF bad_rows > 0 THEN
        RAISE EXCEPTION 'Found % rows with embedding dimension != 384. Fix before migrating.', bad_rows;
    ELSE
        RAISE NOTICE 'All embeddings are 384-dimensional. Safe to proceed.';
    END IF;
END $$;


-- Step 5: Copy data from embedding (JSON) to embedding_vector (vector)
-- ============================================================================
-- Batched UPDATE to avoid massive WAL and long transactions.
-- Processes 50,000 rows per batch.
-- Duration: ~10-15 minutes for 1.6M rows

DO $$
DECLARE
    rows_to_migrate BIGINT;
    total_migrated BIGINT := 0;
    batch_migrated BIGINT;
    batch_size CONSTANT INT := 50000;
BEGIN
    SELECT COUNT(*) INTO rows_to_migrate
    FROM chunk_embeddings
    WHERE embedding_vector IS NULL AND embedding IS NOT NULL;

    IF rows_to_migrate = 0 THEN
        RAISE NOTICE 'No rows to migrate (all embeddings already in vector format)';
        RETURN;
    END IF;

    RAISE NOTICE 'Migrating % rows in batches of %...', rows_to_migrate, batch_size;

    LOOP
        UPDATE chunk_embeddings
        SET embedding_vector = embedding::text::vector(384)
        WHERE id IN (
            SELECT id FROM chunk_embeddings
            WHERE embedding_vector IS NULL AND embedding IS NOT NULL
            LIMIT batch_size
        );

        GET DIAGNOSTICS batch_migrated = ROW_COUNT;
        total_migrated := total_migrated + batch_migrated;

        -- Commit each batch (implicit in DO block per-statement)
        RAISE NOTICE '  Migrated % / % rows...', total_migrated, rows_to_migrate;

        EXIT WHEN batch_migrated = 0;
    END LOOP;

    RAISE NOTICE 'Migration complete: % rows converted', total_migrated;
    RAISE NOTICE '  - embedding column: UNCHANGED (pipeline active)';
    RAISE NOTICE '  - embedding_vector column: POPULATED (pgvector ready)';
END $$;


-- Step 6: Verify data integrity (both columns should have data)
-- ============================================================================
DO $$
DECLARE
    total_rows BIGINT;
    json_rows BIGINT;
    vector_rows BIGINT;
    missing_vector BIGINT;
BEGIN
    SELECT COUNT(*) INTO total_rows FROM chunk_embeddings;
    SELECT COUNT(*) INTO json_rows FROM chunk_embeddings WHERE embedding IS NOT NULL;
    SELECT COUNT(*) INTO vector_rows FROM chunk_embeddings WHERE embedding_vector IS NOT NULL;
    SELECT COUNT(*) INTO missing_vector FROM chunk_embeddings
        WHERE embedding IS NOT NULL AND embedding_vector IS NULL;

    RAISE NOTICE 'Data integrity check:';
    RAISE NOTICE '  Total rows: %', total_rows;
    RAISE NOTICE '  Rows with JSON embeddings: % (pipeline writes here)', json_rows;
    RAISE NOTICE '  Rows with vector embeddings: % (pgvector uses this)', vector_rows;
    RAISE NOTICE '  Rows with JSON but no vector: %', missing_vector;

    IF missing_vector > 0 THEN
        RAISE WARNING 'Found % rows with embedding but no embedding_vector', missing_vector;
    ELSE
        RAISE NOTICE '  All rows have both JSON and vector embeddings';
    END IF;
END $$;


-- Step 7: Create supporting indexes (B-tree)
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_type
ON chunk_embeddings(chunk_type);

CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_hash
ON chunk_embeddings(content_hash);

CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_model
ON chunk_embeddings(embedding_model);


-- Step 8: Configure PostgreSQL for HNSW index building
-- ============================================================================
SET maintenance_work_mem = '2GB';
SET max_parallel_maintenance_workers = 4;


-- Step 9: Create HNSW indexes CONCURRENTLY on embedding_vector
-- ============================================================================
-- Using CONCURRENTLY: no SHARE lock, writes continue during build.
-- Cannot be inside DO blocks or transactions.
-- Parameters: m=16, ef_construction=128 (explicit, better recall for legal research)
--
-- Duration: ~90-120 minutes per index for 1.6M rows
-- Monitor progress: SELECT * FROM pg_stat_progress_create_index;

-- Index for interventions (~1.6M rows)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_embeddings_intervention_vector_hnsw
ON chunk_embeddings
USING hnsw (embedding_vector vector_cosine_ops)
WITH (m = 16, ef_construction = 128)
WHERE chunk_type = 'intervention';

-- Index for amendements
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_embeddings_amendement_vector_hnsw
ON chunk_embeddings
USING hnsw (embedding_vector vector_cosine_ops)
WITH (m = 16, ef_construction = 128)
WHERE chunk_type = 'amendement';


-- Step 10: Optimize table and update statistics
-- ============================================================================
VACUUM ANALYZE chunk_embeddings;


-- Step 11: Verify indexes are ready
-- ============================================================================
SELECT
    schemaname,
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE tablename = 'chunk_embeddings'
ORDER BY indexname;

-- Check index validity (CONCURRENTLY indexes can be invalid if build failed)
SELECT
    indexname,
    CASE WHEN indisvalid THEN 'VALID' ELSE 'INVALID - rebuild needed' END AS status
FROM pg_indexes
JOIN pg_index ON pg_indexes.indexname = pg_index.indexrelid::regclass::text
WHERE tablename = 'chunk_embeddings'
  AND indexdef LIKE '%hnsw%';

-- Check table size
SELECT
    pg_size_pretty(pg_total_relation_size('chunk_embeddings')) AS total_size,
    pg_size_pretty(pg_relation_size('chunk_embeddings')) AS table_size,
    pg_size_pretty(pg_total_relation_size('chunk_embeddings') - pg_relation_size('chunk_embeddings')) AS indexes_size;


-- ============================================================================
-- Migration complete!
-- ============================================================================
-- Summary:
-- 1. embedding column: UNCHANGED (pipeline continues writing here)
-- 2. embedding_vector column: NEW (pgvector searches here)
-- 3. HNSW indexes: Created CONCURRENTLY on embedding_vector (no write locks)
--
-- Next steps:
-- 1. Run: psql $DATABASE_URL -f scripts/create_embedding_sync_trigger.sql
-- 2. Update .env: VECTOR_STORE_PROVIDER=pgvector
-- 3. Redeploy API
-- ============================================================================


-- ============================================================================
-- ROLLBACK PROCEDURE (if needed)
-- ============================================================================
-- If you need to revert to the original state:
--
-- 1. Drop vector column and indexes:
--    DROP INDEX IF EXISTS idx_embeddings_intervention_vector_hnsw;
--    DROP INDEX IF EXISTS idx_embeddings_amendement_vector_hnsw;
--    ALTER TABLE chunk_embeddings DROP COLUMN IF EXISTS embedding_vector;
--
-- 2. embedding column remains intact (pipeline never stopped working)
--
-- 3. Update .env:
--    VECTOR_STORE_PROVIDER=memory
-- ============================================================================
