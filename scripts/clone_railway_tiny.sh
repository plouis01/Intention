#!/bin/bash

# ============================================================================
# Clone TINY sample from Railway (1,000 chunks) - ULTRA FAST
# ============================================================================
# This script creates a minimal test database with only 1,000 chunks
# for rapid testing of pgvector migration.
#
# Duration: ~2 minutes total
# Use case: Quick validation that migration works
# ============================================================================

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}===================================================================="
echo "Railway → Local TINY Sample Clone (1,000 chunks)"
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

LOCAL_DB_NAME="legislateur_tiny"
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
# Step 4: Clone 1,000 chunks (interventions only)
# ============================================================================
echo -e "${YELLOW}Step 4: Cloning 1,000 intervention chunks...${NC}"

# Export from Railway
psql "$DATABASE_URL" -t -c "COPY (
    SELECT * FROM chunk_embeddings
    WHERE chunk_type = 'intervention'
    ORDER BY id DESC
    LIMIT 1000
) TO STDOUT" | psql -h localhost -U $LOCAL_DB_USER -d $LOCAL_DB_NAME \
    -c "COPY chunk_embeddings FROM STDIN"

echo -e "${GREEN}✓ 1,000 chunks imported${NC}\n"

# ============================================================================
# Step 5: Verify import
# ============================================================================
echo -e "${YELLOW}Step 5: Verifying data...${NC}"

LOCAL_DATABASE_URL="postgresql://${LOCAL_DB_USER}@localhost:5432/${LOCAL_DB_NAME}"

COUNT=$(psql "$LOCAL_DATABASE_URL" -t -c "SELECT COUNT(*) FROM chunk_embeddings;")

echo -e "${BLUE}Local database stats:${NC}"
echo -e "  Database name: ${LOCAL_DB_NAME}"
echo -e "  Total chunks:  ${COUNT}"
echo -e "  Connection:    ${LOCAL_DATABASE_URL}\n"

# ============================================================================
# Step 6: Create .env.tiny for testing
# ============================================================================
echo -e "${YELLOW}Step 6: Creating .env.tiny...${NC}"

cat > .env.tiny << EOF
# ============================================================================
# Local TINY Testing Environment (Auto-generated)
# ============================================================================
# Minimal database with 1,000 chunks for rapid pgvector testing
#
# Usage:
#   export \$(grep -v '^#' .env.tiny | xargs)
#   psql \$DATABASE_URL -f scripts/migrate_to_pgvector_safe.sql
# ============================================================================

# Local TINY database (1,000 chunks)
DATABASE_URL=${LOCAL_DATABASE_URL}

# Vector store configuration
VECTOR_STORE_PROVIDER=pgvector

# Copy other settings from .env
$(grep -E '^(EMBEDDING_|LLM_|GROQ_|PISTE_|LOCAL_)' .env 2>/dev/null || echo "# No additional settings")

# ============================================================================
# Testing Notes:
# ============================================================================
# - This is a TINY sample with only 1,000 chunks
# - Migration will take ~10 seconds (vs 2h for full dataset)
# - Index build will take ~10 seconds (vs 90-120 min)
# - Perfect for validating that migration works correctly
# - After validation, run on Railway with full dataset
# ============================================================================
EOF

echo -e "${GREEN}✓ Created .env.tiny${NC}\n"

# ============================================================================
# Summary
# ============================================================================
echo -e "${GREEN}===================================================================="
echo "✓ TINY sample database ready!"
echo -e "====================================================================${NC}\n"

echo -e "${BLUE}Database Details:${NC}"
echo -e "  Name:     ${LOCAL_DB_NAME}"
echo -e "  Chunks:   1,000 (intervention only)"
echo -e "  URL:      ${LOCAL_DATABASE_URL}\n"

echo -e "${YELLOW}Next Steps:${NC}"
echo -e "  1. Test pgvector migration:"
echo -e "     ${GREEN}export \$(grep -v '^#' .env.tiny | xargs)${NC}"
echo -e "     ${GREEN}psql \$DATABASE_URL -f scripts/migrate_to_pgvector_safe.sql${NC}\n"

echo -e "  2. Create sync trigger:"
echo -e "     ${GREEN}psql \$DATABASE_URL -f scripts/create_embedding_sync_trigger.sql${NC}\n"

echo -e "  3. Verify both columns exist:"
echo -e "     ${GREEN}psql \$DATABASE_URL -c \"\\\\d chunk_embeddings\"${NC}\n"

echo -e "  4. Test performance:"
echo -e "     ${GREEN}python scripts/test_pgvector_performance.py${NC}\n"

echo -e "${YELLOW}⏱️  Expected times:${NC}"
echo -e "  - Migration:  ~10 seconds"
echo -e "  - Index HNSW: ~10 seconds"
echo -e "  - Total:      ~30 seconds\n"

echo -e "${GREEN}===================================================================="
echo "Ready to test! 🚀"
echo -e "====================================================================${NC}"
