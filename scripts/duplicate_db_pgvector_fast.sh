#!/bin/bash

# Fast database duplication using custom format with parallel jobs
# Usage: SOURCE_DATABASE_URL=... TARGET_DATABASE_URL=... ./scripts/duplicate_db_pgvector_fast.sh

set -e  # Exit on error

# Database URLs (from environment)
SOURCE_DB="${SOURCE_DATABASE_URL:?Set SOURCE_DATABASE_URL}"
TARGET_DB="${TARGET_DATABASE_URL:?Set TARGET_DATABASE_URL}"

# Configuration
JOBS=4  # Parallel jobs (adjust based on CPU cores)
DUMP_DIR="/tmp/railway_db_$(date +%Y%m%d_%H%M%S)"

echo "============================================================"
echo "FAST DATABASE DUPLICATION: PostgreSQL → pgvector"
echo "============================================================"
echo ""
echo "⚡ Using custom format with $JOBS parallel jobs"
echo ""

# Step 1: Create directory dump with parallel jobs
echo "📦 Step 1: Creating parallel database dump..."
echo "   Source: \$SOURCE_DATABASE_URL"
echo "   Output: $DUMP_DIR/"
echo "   Jobs: $JOBS (parallel)"
echo ""

pg_dump "$SOURCE_DB" \
    --format=directory \
    --jobs=$JOBS \
    --verbose \
    --no-owner \
    --no-acl \
    --file="$DUMP_DIR"

DUMP_SIZE=$(du -sh "$DUMP_DIR" | cut -f1)
echo ""
echo "✅ Parallel dump created: $DUMP_SIZE"
echo ""

# Step 2: Ensure pgvector extension exists on target
echo "📦 Step 2: Installing pgvector extension on target..."
psql "$TARGET_DB" -c "CREATE EXTENSION IF NOT EXISTS vector;" || true
echo "✅ pgvector extension ready"
echo ""

# Step 3: Restore to target database with parallel jobs
echo "📥 Step 3: Restoring to target database..."
echo "   Target: \$TARGET_DATABASE_URL"
echo "   Jobs: $JOBS (parallel restore)"
echo "   ⚠️  This will DROP existing tables in target!"
echo ""
read -p "Continue? (yes/no): " -r
if [[ ! $REPLY =~ ^[Yy]es$ ]]; then
    echo "❌ Aborted by user"
    rm -rf "$DUMP_DIR"
    exit 1
fi

# Drop existing tables in target
echo ""
echo "🧹 Dropping existing tables in target..."
psql "$TARGET_DB" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" || true
echo "✅ Cleaned target database"
echo ""

# Restore dump with parallel jobs
echo "📥 Restoring database dump (parallel)..."
pg_restore \
    --dbname="$TARGET_DB" \
    --jobs=$JOBS \
    --verbose \
    --no-owner \
    --no-acl \
    "$DUMP_DIR"

echo ""
echo "✅ Database restored"
echo ""

# Step 4: Add embedding_vector column
echo "📊 Step 4: Adding embedding_vector column..."
psql "$TARGET_DB" -c "
    ALTER TABLE chunk_embeddings
    ADD COLUMN IF NOT EXISTS embedding_vector vector(384);
"
echo "✅ embedding_vector column added"
echo ""

# Step 5: Verify restoration
echo "🔍 Step 5: Verifying restoration..."
echo ""

TABLES=$(psql "$TARGET_DB" -t -c "
    SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public';
")

echo "   Tables restored: $TABLES"

# Show table counts
psql "$TARGET_DB" -c "
    SELECT
        tablename,
        (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', schemaname, tablename), false, true, '')))[1]::text::int as row_count
    FROM pg_tables
    WHERE schemaname = 'public'
    ORDER BY tablename;
"

# Specific chunk_embeddings count
CHUNK_COUNT=$(psql "$TARGET_DB" -t -c "SELECT COUNT(*) FROM chunk_embeddings;")
echo ""
echo "   📊 chunk_embeddings: $(echo $CHUNK_COUNT | xargs) rows"

# Cleanup
echo ""
echo "🧹 Cleaning up dump directory..."
rm -rf "$DUMP_DIR"
echo "✅ Cleanup complete"

echo ""
echo "============================================================"
echo "✅ FAST DATABASE DUPLICATION COMPLETED"
echo "============================================================"
echo ""
echo "Next steps:"
echo "1. Populate embedding_vector from embedding JSON:"
echo "   python scripts/populate_embedding_vector.py"
echo ""
echo "2. Run delta migration to catch up new chunks:"
echo "   python scripts/migrate_pgvector_delta.py"
echo ""
echo "3. Create HNSW index (on Railway for more RAM):"
echo "   railway run psql \$DATABASE_URL"
echo "   CREATE INDEX CONCURRENTLY idx_chunk_embeddings_vector"
echo "   ON chunk_embeddings"
echo "   USING hnsw (embedding_vector vector_cosine_ops)"
echo "   WITH (m = 16, ef_construction = 64);"
echo ""
echo "4. Update Railway env vars:"
echo "   DATABASE_URL → \${{ Postgres-pgvector.DATABASE_PRIVATE_URL }}"
echo "   VECTOR_STORE_PROVIDER → pgvector"
