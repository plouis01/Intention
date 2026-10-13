#!/bin/bash

# ============================================================================
# Clone SAMPLE from Railway (150,000 chunks) - FAST
# ============================================================================
# This script creates a representative test database with 150,000 chunks
# for realistic testing of pgvector migration.
#
# Duration: ~10 minutes total
# Use case: Realistic validation with significant dataset
# ============================================================================

set -e  # Exit on error

# PostgreSQL binary path
PSQL_PATH="/opt/homebrew/Cellar/postgresql@17/17.8/bin"
export PATH="$PSQL_PATH:$PATH"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}===================================================================="
echo "Railway → Local SAMPLE Clone (150,000 chunks)"
echo -e "====================================================================${NC}\n"

# ============================================================================
# Step 1: Load Railway DATABASE_URL
# ============================================================================
echo -e "${YELLOW}Step 1: Loading Railway connection...${NC}"

if [ ! -f .env ]; then
    echo -e "${RED}✗ .env file not found!${NC}"
    exit 1
fi

set -a
source .env
set +a

if [ -z "$DATABASE_URL" ]; then
    echo -e "${RED}✗ DATABASE_URL not found in .env!${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Railway DATABASE_URL loaded${NC}\n"

# ============================================================================
# Step 2: Create local test database
# ============================================================================
echo -e "${YELLOW}Step 2: Creating local test database...${NC}"

LOCAL_DB_NAME="legislateur_sample"
LOCAL_DB_USER="${USER}"

# Drop if exists and recreate
dropdb -h localhost -U $LOCAL_DB_USER $LOCAL_DB_NAME 2>/dev/null || true
createdb -h localhost -U $LOCAL_DB_USER $LOCAL_DB_NAME

echo -e "${GREEN}✓ Database '${LOCAL_DB_NAME}' created${NC}\n"

# ============================================================================
# Step 3: Clone schema (structure only)
# ============================================================================
echo -e "${YELLOW}Step 3: Cloning table schema...${NC}"

# Ignore version mismatch - dump schema
pg_dump "$DATABASE_URL" \
    --schema-only \
    --no-owner \
    --no-acl \
    2>/dev/null | psql -h localhost -U $LOCAL_DB_USER -d $LOCAL_DB_NAME -q

echo -e "${GREEN}✓ Schema cloned${NC}\n"

# ============================================================================
# Step 4: Clone 150,000 chunks (mix of types)
# ============================================================================
echo -e "${YELLOW}Step 4: Cloning 150,000 chunks (interventions + amendements)...${NC}"

# Export from Railway
psql "$DATABASE_URL" -t -c "COPY (
    SELECT * FROM chunk_embeddings
    ORDER BY id DESC
    LIMIT 150000
) TO STDOUT" | psql -h localhost -U $LOCAL_DB_USER -d $LOCAL_DB_NAME \
    -c "COPY chunk_embeddings FROM STDIN"

echo -e "${GREEN}✓ 150,000 chunks imported${NC}\n"

# ============================================================================
# Step 5: Verify import and show distribution
# ============================================================================
echo -e "${YELLOW}Step 5: Verifying data...${NC}"

LOCAL_DATABASE_URL="postgresql://${LOCAL_DB_USER}@localhost:5432/${LOCAL_DB_NAME}"

echo -e "${BLUE}Local database stats:${NC}"
psql "$LOCAL_DATABASE_URL" -c "
SELECT
    chunk_type,
    COUNT(*) as count,
    pg_size_pretty(SUM(octet_length(content))) as content_size
FROM chunk_embeddings
GROUP BY chunk_type
ORDER BY COUNT(*) DESC;
"

TOTAL=$(psql "$LOCAL_DATABASE_URL" -t -c "SELECT COUNT(*) FROM chunk_embeddings;")
echo -e "\n${BLUE}Total chunks: ${TOTAL}${NC}"
echo -e "${BLUE}Connection:   ${LOCAL_DATABASE_URL}${NC}\n"

# ============================================================================
# Step 6: Create .env.sample for testing
# ============================================================================
echo -e "${YELLOW}Step 6: Creating .env.sample...${NC}"

cat > .env.sample << EOF
# ============================================================================
# Local SAMPLE Testing Environment (Auto-generated)
# ============================================================================
# Representative database with 150,000 chunks for realistic pgvector testing
#
# Usage:
#   export \$(grep -v '^#' .env.sample | xargs)
#   psql \$DATABASE_URL -f scripts/migrate_to_pgvector_safe.sql
# ============================================================================

# Local SAMPLE database (150,000 chunks)
DATABASE_URL=${LOCAL_DATABASE_URL}

# Vector store configuration
VECTOR_STORE_PROVIDER=pgvector

# Copy other settings from .env
$(grep -E '^(EMBEDDING_|LLM_|GROQ_|PISTE_|LOCAL_)' .env 2>/dev/null || echo "# No additional settings")

# ============================================================================
# Testing Notes:
# ============================================================================
# - This is a SAMPLE with 150,000 chunks (~8% of full dataset)
# - Migration will take ~2-3 minutes
# - Index build will take ~15-20 minutes (vs 90-120 min for full)
# - More realistic than tiny sample for performance testing
# - After validation, run on Railway with full dataset
# ============================================================================
EOF

echo -e "${GREEN}✓ Created .env.sample${NC}\n"

# ============================================================================
# Summary
# ============================================================================
echo -e "${GREEN}===================================================================="
echo "✓ SAMPLE database ready!"
echo -e "====================================================================${NC}\n"

echo -e "${BLUE}Database Details:${NC}"
echo -e "  Name:     ${LOCAL_DB_NAME}"
echo -e "  Chunks:   150,000 (mixed types)"
echo -e "  URL:      ${LOCAL_DATABASE_URL}\n"

echo -e "${YELLOW}Next Steps:${NC}"
echo -e "  1. Test pgvector migration:"
echo -e "     ${GREEN}export \$(grep -v '^#' .env.sample | xargs)${NC}"
echo -e "     ${GREEN}psql \$DATABASE_URL -f scripts/migrate_to_pgvector_safe.sql${NC}\n"

echo -e "  2. Monitor progress (in separate terminal):"
echo -e "     ${GREEN}export \$(grep -v '^#' .env.sample | xargs)${NC}"
echo -e "     ${GREEN}watch -n 10 'psql \$DATABASE_URL -f scripts/monitor_migration.sql'${NC}\n"

echo -e "  3. Create sync trigger:"
echo -e "     ${GREEN}psql \$DATABASE_URL -f scripts/create_embedding_sync_trigger.sql${NC}\n"

echo -e "  4. Test performance:"
echo -e "     ${GREEN}python scripts/test_pgvector_performance.py${NC}\n"

echo -e "${YELLOW}⏱️  Expected times:${NC}"
echo -e "  - Clone:      ~5 minutes (done)"
echo -e "  - Migration:  ~2-3 minutes"
echo -e "  - Index HNSW: ~15-20 minutes"
echo -e "  - Total:      ~20-30 minutes\n"

echo -e "${GREEN}===================================================================="
echo "Ready for realistic testing! 🚀"
echo -e "====================================================================${NC}"
