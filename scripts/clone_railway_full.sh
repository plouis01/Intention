#!/bin/bash

# ============================================================================
# Clone FULL database from Railway (ALL chunks) - COMPLETE TEST
# ============================================================================
# This script creates a complete local copy of the Railway database
# for full-scale pgvector migration testing.
#
# Duration: ~30-45 minutes total
# Use case: Final validation before Railway migration
# ============================================================================

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}===================================================================="
echo "Railway → Local FULL Clone (ALL chunks)"
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
# Step 2: Count total chunks on Railway
# ============================================================================
echo -e "${YELLOW}Step 2: Counting chunks on Railway...${NC}"

TOTAL_CHUNKS=$(psql "$DATABASE_URL" -t -c "SELECT COUNT(*) FROM chunk_embeddings;")
TOTAL_CHUNKS=$(echo $TOTAL_CHUNKS | xargs)  # Trim whitespace

echo -e "${BLUE}Total chunks on Railway: ${TOTAL_CHUNKS}${NC}"
echo -e "${YELLOW}⏱️  Estimated time: ~30-45 minutes${NC}\n"

# ============================================================================
# Step 3: Create local test database
# ============================================================================
echo -e "${YELLOW}Step 3: Creating local test database...${NC}"

LOCAL_DB_NAME="legislateur_full"
LOCAL_DB_USER="${USER}"

# Drop if exists and recreate
dropdb -h localhost -U $LOCAL_DB_USER $LOCAL_DB_NAME 2>/dev/null || true
createdb -h localhost -U $LOCAL_DB_USER $LOCAL_DB_NAME

echo -e "${GREEN}✓ Database '${LOCAL_DB_NAME}' created${NC}\n"

# ============================================================================
# Step 4: Clone schema (structure only)
# ============================================================================
echo -e "${YELLOW}Step 4: Cloning table schema...${NC}"

# Ignore version mismatch - dump schema
pg_dump "$DATABASE_URL" \
    --schema-only \
    --no-owner \
    --no-acl \
    2>/dev/null | psql -h localhost -U $LOCAL_DB_USER -d $LOCAL_DB_NAME -q

echo -e "${GREEN}✓ Schema cloned${NC}\n"

# ============================================================================
# Step 5: Clone ALL chunks (FULL database)
# ============================================================================
echo -e "${YELLOW}Step 5: Cloning ALL chunks from Railway...${NC}"
echo -e "${YELLOW}This will take 30-45 minutes depending on connection speed...${NC}\n"

# Start time
START_TIME=$(date +%s)

# Export from Railway using COPY (fastest method)
psql "$DATABASE_URL" -c "COPY chunk_embeddings TO STDOUT" | \
    psql -h localhost -U $LOCAL_DB_USER -d $LOCAL_DB_NAME \
    -c "COPY chunk_embeddings FROM STDIN"

# End time
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
MINUTES=$((DURATION / 60))
SECONDS=$((DURATION % 60))

echo -e "\n${GREEN}✓ ALL chunks imported in ${MINUTES}m ${SECONDS}s${NC}\n"

# ============================================================================
# Step 6: Verify import
# ============================================================================
echo -e "${YELLOW}Step 6: Verifying data...${NC}"

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
# Step 7: Create .env.full for testing
# ============================================================================
echo -e "${YELLOW}Step 7: Creating .env.full...${NC}"

cat > .env.full << EOF
# ============================================================================
# Local FULL Testing Environment (Auto-generated)
# ============================================================================
# Complete database clone from Railway for full-scale pgvector testing
#
# Usage:
#   export \$(grep -v '^#' .env.full | xargs)
#   psql \$DATABASE_URL -f scripts/migrate_to_pgvector_safe.sql
# ============================================================================

# Local FULL database (${TOTAL_CHUNKS} chunks)
DATABASE_URL=${LOCAL_DATABASE_URL}

# Vector store configuration
VECTOR_STORE_PROVIDER=pgvector

# Copy other settings from .env
$(grep -E '^(EMBEDDING_|LLM_|GROQ_|PISTE_|LOCAL_)' .env 2>/dev/null || echo "# No additional settings")

# ============================================================================
# Testing Notes:
# ============================================================================
# - This is a FULL clone with ${TOTAL_CHUNKS} chunks
# - Migration will take ~15-20 minutes
# - Index build will take ~90-120 minutes (HNSW on 1.6M vectors)
# - This is the final test before applying to Railway production
# - Performance will match Railway after migration
# ============================================================================
EOF

echo -e "${GREEN}✓ Created .env.full${NC}\n"

# ============================================================================
# Summary
# ============================================================================
echo -e "${GREEN}===================================================================="
echo "✓ FULL database clone ready!"
echo -e "====================================================================${NC}\n"

echo -e "${BLUE}Database Details:${NC}"
echo -e "  Name:     ${LOCAL_DB_NAME}"
echo -e "  Chunks:   ${TOTAL_CHUNKS}"
echo -e "  URL:      ${LOCAL_DATABASE_URL}\n"

echo -e "${YELLOW}Next Steps:${NC}"
echo -e "  1. Test pgvector migration on FULL database:\n"
echo -e "     ${GREEN}export \$(grep -v '^#' .env.full | xargs)${NC}"
echo -e "     ${GREEN}psql \$DATABASE_URL -f scripts/migrate_to_pgvector_safe.sql${NC}\n"

echo -e "  2. Monitor progress (in separate terminal):\n"
echo -e "     ${GREEN}export \$(grep -v '^#' .env.full | xargs)${NC}"
echo -e "     ${GREEN}watch -n 10 'psql \$DATABASE_URL -c \"SELECT * FROM pg_stat_progress_create_index;\"'${NC}\n"

echo -e "  3. Create sync trigger:\n"
echo -e "     ${GREEN}psql \$DATABASE_URL -f scripts/create_embedding_sync_trigger.sql${NC}\n"

echo -e "  4. Run accuracy verification:\n"
echo -e "     ${GREEN}python scripts/verify_pgvector_accuracy.py${NC}\n"

echo -e "  5. Run performance benchmark:\n"
echo -e "     ${GREEN}python scripts/benchmark_json_vs_pgvector.py${NC}\n"

echo -e "${YELLOW}⏱️  Expected times (FULL database):${NC}"
echo -e "  - Clone:      ${MINUTES}m ${SECONDS}s (done ✅)"
echo -e "  - Migration:  ~15-20 minutes"
echo -e "  - Index HNSW: ~90-120 minutes"
echo -e "  - Total:      ~2-2.5 hours\n"

echo -e "${GREEN}===================================================================="
echo "Ready for FULL-SCALE testing! 🚀"
echo -e "====================================================================${NC}"
