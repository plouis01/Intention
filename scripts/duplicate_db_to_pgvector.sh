#!/bin/bash

# Script to duplicate entire PostgreSQL database to new pgvector database
# Usage: SOURCE_DATABASE_URL=... TARGET_DATABASE_URL=... ./scripts/duplicate_db_to_pgvector.sh

set -e  # Exit on error

# Database URLs (from environment)
SOURCE_DB="${SOURCE_DATABASE_URL:?Set SOURCE_DATABASE_URL}"
TARGET_DB="${TARGET_DATABASE_URL:?Set TARGET_DATABASE_URL}"

echo "============================================================"
echo "DATABASE DUPLICATION: PostgreSQL → pgvector"
echo "============================================================"
echo ""

# Step 1: Create dump file
DUMP_FILE="/tmp/railway_db_dump_$(date +%Y%m%d_%H%M%S).sql"
echo "📦 Step 1: Creating database dump..."
echo "   Source: \$SOURCE_DATABASE_URL"
echo "   Output: $DUMP_FILE"
echo ""

pg_dump "$SOURCE_DB" \
    --verbose \
    --no-owner \
    --no-acl \
    --format=plain \
    --serializable-deferrable \
    --file="$DUMP_FILE"

DUMP_SIZE=$(du -h "$DUMP_FILE" | cut -f1)
echo ""
echo "✅ Dump created: $DUMP_SIZE"
echo ""

# Step 2: Ensure pgvector extension exists on target
echo "📦 Step 2: Installing pgvector extension on target..."
psql "$TARGET_DB" -c "CREATE EXTENSION IF NOT EXISTS vector;" || true
echo "✅ pgvector extension ready"
echo ""

# Step 3: Restore to target database
echo "📥 Step 3: Restoring to target database..."
echo "   Target: \$TARGET_DATABASE_URL"
echo "   ⚠️  This will DROP existing tables in target!"
echo ""
read -p "Continue? (yes/no): " -r
if [[ ! $REPLY =~ ^[Yy]es$ ]]; then
    echo "❌ Aborted by user"
    rm "$DUMP_FILE"
    exit 1
fi

# Drop existing tables in target
echo ""
echo "🧹 Dropping existing tables in target..."
psql "$TARGET_DB" -c "DROP TABLE IF EXISTS chunk_embeddings CASCADE;" || true
echo "✅ Cleaned target database"
echo ""

# Restore dump
echo "📥 Restoring database dump..."
psql "$TARGET_DB" < "$DUMP_FILE"

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
        (SELECT COUNT(*) FROM pg_class WHERE relname = tablename)::text as row_count
    FROM pg_tables
    WHERE schemaname = 'public'
    ORDER BY tablename;
"

# Cleanup
echo ""
echo "🧹 Cleaning up dump file..."
rm "$DUMP_FILE"
echo "✅ Cleanup complete"

echo ""
echo "============================================================"
echo "✅ DATABASE DUPLICATION COMPLETED"
echo "============================================================"
echo ""
echo "Next steps:"
echo "1. Populate embedding_vector from embedding JSON:"
echo "   python scripts/populate_embedding_vector.py"
echo ""
echo "2. Create HNSW index:"
echo "   python scripts/create_hnsw_index.py"
echo ""
echo "3. Update Railway env vars:"
echo "   DATABASE_URL → \${{ Postgres-pgvector.DATABASE_PRIVATE_URL }}"
echo "   VECTOR_STORE_PROVIDER → pgvector"
