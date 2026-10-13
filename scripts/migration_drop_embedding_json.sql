-- =============================================================================
-- MIGRATION: Drop embedding JSON column from chunk_embeddings
-- =============================================================================
--
-- PURPOSE:
--   Remove the redundant `embedding` JSON column (~21 GB TOAST data) now that
--   the pipeline writes directly to `embedding_vector` (pgvector format).
--
-- PREREQUISITES:
--   1. Take a full database backup BEFORE running this script
--   2. Verify no NULL values in embedding_vector:
--        SELECT COUNT(*) FROM chunk_embeddings WHERE embedding_vector IS NULL;
--      Expected result: 0
--   3. Confirm the application code has been deployed with the new direct-write
--      pipeline (no code references to the `embedding` column remain)
--
-- WARNING:
--   THIS MIGRATION IS IRREVERSIBLE. The embedding JSON data cannot be recovered
--   after this script runs. Ensure you have a verified backup.
--
-- EXECUTION:
--   MANUAL ONLY - Do NOT run this automatically.
--   psql $DATABASE_URL -f scripts/migration_drop_embedding_json.sql
--
-- =============================================================================

BEGIN;

-- Step 1: Drop the sync trigger (no longer needed)
DROP TRIGGER IF EXISTS embedding_sync_trigger ON chunk_embeddings;

-- Step 2: Drop the sync function
DROP FUNCTION IF EXISTS sync_embedding_to_vector();

-- Step 3: Drop the embedding JSON column
ALTER TABLE chunk_embeddings DROP COLUMN IF EXISTS embedding;

COMMIT;

-- Step 4: Reclaim disk space (runs outside transaction)
-- This may take several minutes depending on table size
VACUUM FULL chunk_embeddings;

-- Step 5: Verify results
SELECT
    pg_size_pretty(pg_total_relation_size('chunk_embeddings')) AS total_size,
    pg_size_pretty(pg_relation_size('chunk_embeddings')) AS table_size,
    pg_size_pretty(pg_total_relation_size('chunk_embeddings') - pg_relation_size('chunk_embeddings')) AS index_size,
    COUNT(*) AS row_count
FROM chunk_embeddings;
