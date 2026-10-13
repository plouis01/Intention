-- ============================================================================
-- Monitor pgvector migration progress
-- ============================================================================
-- Run this script in a separate terminal while migrate_to_pgvector_safe.sql runs
-- Usage: watch -n 30 'psql $DATABASE_URL -f scripts/monitor_migration.sql'
-- ============================================================================

\echo '===================================================================='
\echo 'pgvector Migration Monitoring Dashboard'
\echo '===================================================================='
\echo ''

-- 1. Check if pgvector extension is enabled
\echo '1. pgvector Extension Status:'
\echo '--------------------------------------------------------------------'
SELECT
    extname AS "Extension",
    extversion AS "Version",
    CASE WHEN extname = 'vector' THEN 'Enabled' ELSE 'Not found' END AS "Status"
FROM pg_extension
WHERE extname = 'vector';
\echo ''

-- 2. Check embedding columns
\echo '2. Embedding Columns:'
\echo '--------------------------------------------------------------------'
SELECT
    column_name AS "Column",
    data_type AS "Type",
    udt_name AS "UDT Name",
    CASE
        WHEN column_name = 'embedding' THEN 'Original (pipeline writes here)'
        WHEN column_name = 'embedding_vector' AND udt_name = 'vector' THEN 'New pgvector column'
        ELSE 'Other'
    END AS "Purpose"
FROM information_schema.columns
WHERE table_name = 'chunk_embeddings'
  AND column_name IN ('embedding', 'embedding_vector')
ORDER BY column_name;
\echo ''

-- 3. Data migration progress (embedding -> embedding_vector)
\echo '3. Data Migration Progress:'
\echo '--------------------------------------------------------------------'
SELECT
    COUNT(*) AS "Total Rows",
    COUNT(*) FILTER (WHERE embedding_vector IS NOT NULL) AS "Migrated (vector)",
    COUNT(*) FILTER (WHERE embedding IS NOT NULL AND embedding_vector IS NULL) AS "Pending",
    CASE
        WHEN COUNT(*) FILTER (WHERE embedding IS NOT NULL) = 0 THEN '0%'
        ELSE ROUND(
            100.0 * COUNT(*) FILTER (WHERE embedding_vector IS NOT NULL)
            / NULLIF(COUNT(*) FILTER (WHERE embedding IS NOT NULL), 0), 2
        ) || '%'
    END AS "Progress"
FROM chunk_embeddings;
\echo ''

-- 4. Index creation progress (REAL-TIME)
\echo '4. Index Build Progress (HNSW):'
\echo '--------------------------------------------------------------------'
SELECT
    a.query,
    p.phase,
    p.blocks_total,
    p.blocks_done,
    ROUND(100.0 * p.blocks_done / NULLIF(p.blocks_total, 0), 2) || '%' AS "Progress",
    p.tuples_total,
    p.tuples_done,
    ROUND(100.0 * p.tuples_done / NULLIF(p.tuples_total, 0), 2) || '%' AS "Tuples %"
FROM pg_stat_progress_create_index p
JOIN pg_stat_activity a ON p.pid = a.pid;
\echo ''
\echo 'If no rows above, index build is either not started or completed.'
\echo ''

-- 5. Existing indexes
\echo '5. Indexes on chunk_embeddings:'
\echo '--------------------------------------------------------------------'
SELECT
    indexname AS "Index Name",
    CASE
        WHEN indexdef LIKE '%hnsw%' THEN 'HNSW (pgvector)'
        WHEN indexdef LIKE '%btree%' OR indexdef NOT LIKE '%hnsw%' THEN 'B-tree'
    END AS "Type",
    CASE
        WHEN indisvalid THEN 'Valid'
        ELSE 'Building...'
    END AS "Status"
FROM pg_indexes
JOIN pg_index ON pg_indexes.indexname = pg_index.indexrelid::regclass::text
WHERE tablename = 'chunk_embeddings'
ORDER BY indexname;
\echo ''

-- 6. Table and indexes size
\echo '6. Storage Usage:'
\echo '--------------------------------------------------------------------'
SELECT
    pg_size_pretty(pg_relation_size('chunk_embeddings')) AS "Table Size",
    pg_size_pretty(pg_total_relation_size('chunk_embeddings') - pg_relation_size('chunk_embeddings')) AS "Indexes Size",
    pg_size_pretty(pg_total_relation_size('chunk_embeddings')) AS "Total Size";
\echo ''

-- 7. Chunk type distribution
\echo '7. Chunk Type Distribution:'
\echo '--------------------------------------------------------------------'
SELECT
    chunk_type AS "Chunk Type",
    COUNT(*) AS "Count",
    pg_size_pretty(SUM(octet_length(content))) AS "Content Size",
    COUNT(*) FILTER (WHERE embedding_vector IS NOT NULL) AS "With Vector"
FROM chunk_embeddings
GROUP BY chunk_type
ORDER BY COUNT(*) DESC;
\echo ''

-- 8. Recent activity
\echo '8. Recent Database Activity:'
\echo '--------------------------------------------------------------------'
SELECT
    query_start AS "Time",
    state AS "State",
    LEFT(query, 100) AS "Query (truncated)"
FROM pg_stat_activity
WHERE query LIKE '%chunk_embeddings%'
  AND query NOT LIKE '%pg_stat_activity%'
ORDER BY query_start DESC
LIMIT 5;
\echo ''

-- 9. Estimated completion time
\echo '9. Estimated Time Remaining:'
\echo '--------------------------------------------------------------------'
DO $$
DECLARE
    blocks_done BIGINT;
    blocks_total BIGINT;
    progress_pct NUMERIC;
    start_time TIMESTAMP;
    elapsed_sec NUMERIC;
    est_remaining_sec NUMERIC;
BEGIN
    SELECT
        p.blocks_done, p.blocks_total, a.query_start
    INTO blocks_done, blocks_total, start_time
    FROM pg_stat_progress_create_index p
    JOIN pg_stat_activity a ON p.pid = a.pid
    LIMIT 1;

    IF blocks_total IS NOT NULL AND blocks_total > 0 THEN
        progress_pct := 100.0 * blocks_done / blocks_total;
        elapsed_sec := EXTRACT(EPOCH FROM (now() - start_time));

        IF progress_pct > 0 THEN
            est_remaining_sec := elapsed_sec * (100.0 / progress_pct - 1);
            RAISE NOTICE 'Index build: %.1f%% complete', progress_pct;
            RAISE NOTICE 'Elapsed: % min, Estimated remaining: % min',
                ROUND((elapsed_sec / 60)::numeric, 1),
                ROUND((est_remaining_sec / 60)::numeric, 1);
        ELSE
            RAISE NOTICE 'Index build starting (0%% complete)...';
        END IF;
    ELSE
        RAISE NOTICE 'No index build currently in progress';
    END IF;
END $$;
\echo ''

\echo '===================================================================='
\echo 'Refresh: watch -n 30 psql $DATABASE_URL -f scripts/monitor_migration.sql'
\echo '===================================================================='
