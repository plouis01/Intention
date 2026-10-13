#!/bin/bash

# ============================================================================
# Clone Railway PostgreSQL database to local PostgreSQL
# ============================================================================
# This script dumps the entire Railway database and restores it to a local
# PostgreSQL instance for safe testing before applying changes to production.
#
# Prerequisites:
#   1. PostgreSQL installed locally (brew install postgresql@16)
#   2. Railway CLI installed (optional, for easy connection)
#   3. Railway DATABASE_URL in .env file
#
# Usage:
#   ./scripts/clone_railway_db_to_local.sh
# ============================================================================

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}===================================================================="
echo "Railway → Local PostgreSQL Database Clone"
echo -e "====================================================================${NC}\n"

# ============================================================================
# Step 1: Check prerequisites
# ============================================================================
echo -e "${YELLOW}Step 1: Checking prerequisites...${NC}"

# Check if PostgreSQL is installed locally
if ! command -v psql &> /dev/null; then
    echo -e "${RED}✗ PostgreSQL not found!${NC}"
    echo "Install with: brew install postgresql@16"
    exit 1
fi
echo -e "${GREEN}✓ PostgreSQL installed${NC}"

# Check if pg_dump is available
if ! command -v pg_dump &> /dev/null; then
    echo -e "${RED}✗ pg_dump not found!${NC}"
    exit 1
fi
echo -e "${GREEN}✓ pg_dump available${NC}"

# Check if .env file exists
if [ ! -f .env ]; then
    echo -e "${RED}✗ .env file not found!${NC}"
    echo "Create .env with DATABASE_URL from Railway"
    exit 1
fi
echo -e "${GREEN}✓ .env file found${NC}\n"

# ============================================================================
# Step 2: Load Railway DATABASE_URL
# ============================================================================
echo -e "${YELLOW}Step 2: Loading Railway database connection...${NC}"

# Load environment variables from .env
export $(grep -v '^#' .env | xargs)

if [ -z "$DATABASE_URL" ]; then
    echo -e "${RED}✗ DATABASE_URL not found in .env!${NC}"
    echo "Add your Railway DATABASE_URL to .env file"
    exit 1
fi

# Parse Railway DATABASE_URL to verify connection
echo -e "${GREEN}✓ Railway DATABASE_URL loaded${NC}"

# Extract database name from Railway URL for logging
RAILWAY_DB_NAME=$(echo $DATABASE_URL | sed -n 's/.*\/\([^?]*\).*/\1/p')
echo -e "${BLUE}  Railway database: ${RAILWAY_DB_NAME}${NC}\n"

# ============================================================================
# Step 3: Configure local PostgreSQL database
# ============================================================================
echo -e "${YELLOW}Step 3: Configuring local PostgreSQL database...${NC}"

# Local database configuration
LOCAL_DB_NAME="legislateur_local"
LOCAL_DB_USER="${USER}"  # Current macOS user
LOCAL_HOST="localhost"
LOCAL_PORT="5432"

echo -e "${BLUE}  Local database name: ${LOCAL_DB_NAME}${NC}"
echo -e "${BLUE}  Local user: ${LOCAL_DB_USER}${NC}\n"

# Check if PostgreSQL service is running
if ! pg_isready -h $LOCAL_HOST -p $LOCAL_PORT &> /dev/null; then
    echo -e "${YELLOW}⚠ PostgreSQL service not running. Starting...${NC}"

    # Try to start PostgreSQL (macOS with Homebrew)
    if command -v brew &> /dev/null; then
        brew services start postgresql@16 || brew services start postgresql
        sleep 3  # Wait for service to start

        if ! pg_isready -h $LOCAL_HOST -p $LOCAL_PORT &> /dev/null; then
            echo -e "${RED}✗ Failed to start PostgreSQL${NC}"
            echo "Start manually: brew services start postgresql@16"
            exit 1
        fi
    else
        echo -e "${RED}✗ PostgreSQL not running${NC}"
        echo "Start manually: pg_ctl -D /usr/local/var/postgresql@16 start"
        exit 1
    fi
fi
echo -e "${GREEN}✓ PostgreSQL service running${NC}"

# Drop existing local database if exists (with confirmation)
if psql -h $LOCAL_HOST -U $LOCAL_DB_USER -lqt | cut -d \| -f 1 | grep -qw $LOCAL_DB_NAME; then
    echo -e "${YELLOW}⚠ Local database '${LOCAL_DB_NAME}' already exists${NC}"
    read -p "Drop and recreate? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${BLUE}  Dropping existing database...${NC}"
        dropdb -h $LOCAL_HOST -U $LOCAL_DB_USER $LOCAL_DB_NAME || true
        echo -e "${GREEN}✓ Existing database dropped${NC}"
    else
        echo -e "${RED}✗ Aborted by user${NC}"
        exit 1
    fi
fi

# Create local database
echo -e "${BLUE}  Creating local database '${LOCAL_DB_NAME}'...${NC}"
createdb -h $LOCAL_HOST -U $LOCAL_DB_USER $LOCAL_DB_NAME
echo -e "${GREEN}✓ Local database created${NC}\n"

# ============================================================================
# Step 4: Dump Railway database
# ============================================================================
echo -e "${YELLOW}Step 4: Dumping Railway database (this may take 5-15 minutes)...${NC}"

DUMP_FILE="data/railway_dump_$(date +%Y%m%d_%H%M%S).sql"
mkdir -p data

echo -e "${BLUE}  Dump file: ${DUMP_FILE}${NC}"
echo -e "${BLUE}  Dumping schema and data...${NC}\n"

# Dump with progress (exclude ownership to avoid permission issues)
pg_dump "$DATABASE_URL" \
    --verbose \
    --no-owner \
    --no-acl \
    --format=plain \
    --file="$DUMP_FILE" \
    2>&1 | grep -v "^pg_dump:" || true  # Filter out verbose output

if [ ! -f "$DUMP_FILE" ]; then
    echo -e "${RED}✗ Dump failed!${NC}"
    exit 1
fi

DUMP_SIZE=$(du -h "$DUMP_FILE" | cut -f1)
echo -e "${GREEN}✓ Database dumped successfully (${DUMP_SIZE})${NC}\n"

# ============================================================================
# Step 5: Restore to local database
# ============================================================================
echo -e "${YELLOW}Step 5: Restoring to local database (this may take 5-15 minutes)...${NC}"

echo -e "${BLUE}  Restoring schema and data...${NC}\n"

# Restore dump to local database
psql -h $LOCAL_HOST -U $LOCAL_DB_USER -d $LOCAL_DB_NAME \
    --quiet \
    --file="$DUMP_FILE" \
    2>&1 | grep -v "^psql:" || true

echo -e "${GREEN}✓ Database restored successfully${NC}\n"

# ============================================================================
# Step 6: Verify restoration
# ============================================================================
echo -e "${YELLOW}Step 6: Verifying data integrity...${NC}"

# Get row counts from local database
LOCAL_COUNTS=$(psql -h $LOCAL_HOST -U $LOCAL_DB_USER -d $LOCAL_DB_NAME -t -c "
    SELECT
        'lois: ' || COUNT(*) FROM lois
    UNION ALL
    SELECT 'articles: ' || COUNT(*) FROM articles
    UNION ALL
    SELECT 'dossiers: ' || COUNT(*) FROM dossiers_legislatifs
    UNION ALL
    SELECT 'debats: ' || COUNT(*) FROM debats
    UNION ALL
    SELECT 'interventions: ' || COUNT(*) FROM interventions
    UNION ALL
    SELECT 'amendements: ' || COUNT(*) FROM amendements
    UNION ALL
    SELECT 'chunk_embeddings: ' || COUNT(*) FROM chunk_embeddings
" | sed 's/^[ \t]*//')

echo -e "${BLUE}Local database row counts:${NC}"
echo "$LOCAL_COUNTS" | while read line; do
    echo -e "${GREEN}  ✓ ${line}${NC}"
done
echo

# ============================================================================
# Step 7: Update local .env for testing
# ============================================================================
echo -e "${YELLOW}Step 7: Creating local test environment...${NC}"

# Create .env.local for testing
LOCAL_DATABASE_URL="postgresql://${LOCAL_DB_USER}@${LOCAL_HOST}:${LOCAL_PORT}/${LOCAL_DB_NAME}"

cat > .env.local << EOF
# ============================================================================
# Local Testing Environment (Auto-generated)
# ============================================================================
# Use this environment file for local testing with cloned Railway database
#
# Usage:
#   export \$(grep -v '^#' .env.local | xargs)
#   python -m src.api.main
# ============================================================================

# Local PostgreSQL database (cloned from Railway)
DATABASE_URL=${LOCAL_DATABASE_URL}

# Vector store configuration (test pgvector locally before Railway)
VECTOR_STORE_PROVIDER=pgvector

# Copy other settings from .env
$(grep -E '^(EMBEDDING_|LLM_|GROQ_|PISTE_|LOCAL_)' .env || echo "# No additional settings found in .env")

# ============================================================================
# Testing Notes:
# ============================================================================
# 1. This database is a SNAPSHOT of Railway at $(date)
# 2. Changes here will NOT affect Railway production
# 3. Re-run clone script to sync latest Railway data
# 4. Test pgvector migration with: psql \$DATABASE_URL -f scripts/migrate_to_pgvector.sql
# ============================================================================
EOF

echo -e "${GREEN}✓ Created .env.local for testing${NC}"
echo -e "${BLUE}  Local DATABASE_URL: ${LOCAL_DATABASE_URL}${NC}\n"

# ============================================================================
# Step 8: Summary and next steps
# ============================================================================
echo -e "${GREEN}===================================================================="
echo "✓ Railway database successfully cloned to local PostgreSQL!"
echo -e "====================================================================${NC}\n"

echo -e "${BLUE}Local Database Details:${NC}"
echo -e "  Name:     ${LOCAL_DB_NAME}"
echo -e "  User:     ${LOCAL_DB_USER}"
echo -e "  Host:     ${LOCAL_HOST}:${LOCAL_PORT}"
echo -e "  URL:      ${LOCAL_DATABASE_URL}\n"

echo -e "${BLUE}Dump File:${NC}"
echo -e "  Location: ${DUMP_FILE}"
echo -e "  Size:     ${DUMP_SIZE}\n"

echo -e "${YELLOW}Next Steps:${NC}"
echo -e "  1. Test pgvector migration locally:"
echo -e "     ${GREEN}export \$(grep -v '^#' .env.local | xargs)${NC}"
echo -e "     ${GREEN}psql \$DATABASE_URL -f scripts/migrate_to_pgvector.sql${NC}\n"

echo -e "  2. Monitor migration progress:"
echo -e "     ${GREEN}psql \$DATABASE_URL -f scripts/monitor_migration.sql${NC}\n"

echo -e "  3. Test API with local database:"
echo -e "     ${GREEN}export \$(grep -v '^#' .env.local | xargs)${NC}"
echo -e "     ${GREEN}python -m src.api.main${NC}\n"

echo -e "  4. Connect with psql:"
echo -e "     ${GREEN}psql ${LOCAL_DATABASE_URL}${NC}\n"

echo -e "  5. When satisfied, apply to Railway:"
echo -e "     ${GREEN}psql \$DATABASE_URL -f scripts/migrate_to_pgvector.sql${NC}"
echo -e "     ${YELLOW}(Use Railway DATABASE_URL from .env)${NC}\n"

echo -e "${YELLOW}⚠️  Important:${NC}"
echo -e "  - Local database is a SNAPSHOT (not synced with Railway)"
echo -e "  - Re-run this script to sync latest Railway data"
echo -e "  - Keep dump file (${DUMP_FILE}) as backup\n"

echo -e "${GREEN}===================================================================="
echo "Happy testing! 🚀"
echo -e "====================================================================${NC}"
