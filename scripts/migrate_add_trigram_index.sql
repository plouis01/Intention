-- Migration: Add trigram index for ILIKE performance on chunk_embeddings.content
-- This speeds up ILIKE queries from ~500ms to ~50ms on 3M rows.
-- Run with: psql $DATABASE_URL -f scripts/migrate_add_trigram_index.sql

-- Enable pg_trgm extension (usually already available on Railway)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Create GIN trigram index on content column (CONCURRENTLY to avoid locking)
-- PostgreSQL automatically uses this index for ILIKE queries.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunk_content_trgm
ON chunk_embeddings USING gin(content gin_trgm_ops);
