# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

"Intention du Législateur" - A legal tech tool for French tax lawyers (avocats fiscalistes) to analyze legislative intent behind French Finance Laws (LF, LFR, LFSS) from 2012-2025 by extracting and analyzing parliamentary debates, amendments, and reports.

## Commands

```bash
# Install dependencies
pip install -e .
pip install -e ".[dev]"      # With dev tools (pytest, ruff)
pip install -e ".[qdrant]"   # With Qdrant vector store

# Run full pipeline (7 phases: collect → import → link → fix → embed → index → validate)
python scripts/run_full_pipeline.py --all
python scripts/run_full_pipeline.py --collect --import --validate
python scripts/run_full_pipeline.py --link --fix
python scripts/run_full_pipeline.py --embed --index
python scripts/run_full_pipeline.py --reset --import  # Reset DB before import

# Run API server
python -m src.api.main                              # Dev server on localhost:8000
uvicorn src.api.main:app --reload                   # Alternative with auto-reload

# Run tests
pytest tests/                                       # All tests
pytest tests/test_api.py -v                         # Single test file
pytest tests/test_indexing.py::test_embedding -v   # Single test

# Linting
ruff check src/ tests/
ruff check --fix src/                               # Auto-fix issues

# Article analysis scripts
python scripts/analyse_article.py "150-0 B ter"     # Analyze single CGI article
python scripts/analyse_loi.py 2023-1322             # Analyze law by number
python scripts/batch_analyse.py                     # Batch analysis
```

## Architecture

### Data Flow

```
Data Sources → Collectors → Raw JSON/XML → Parsers → SQLite DB → Embeddings → Vector Store → Search → LLM Analysis
```

### Pipeline Phases (run_full_pipeline.py)

1. **COLLECT**: Core collectors (Légifrance, AN, Sénat) + DILA archives + missing law handling
2. **IMPORT**: Core import + DILA debates + Sénat debates + external sources
3. **LINK**: Debate-dossier linking + orphaned debates + amendment-article linking
4. **FIX**: Encoding fixes for interventions and external sources
5. **EMBED**: Embeddings generation + term expansions (requires API keys)
6. **INDEX**: Generate article modification index for analysis
7. **VALIDATE**: Record counts and completeness checks

### Core Modules

**Data Collection (`src/collectors/`)**

- Fetches from Légifrance API (OAuth), AN open data, Sénat archives
- Handles rate limiting with retry logic

**Search & Analysis (`src/indexing/`, `src/analysis/`)**

- Hybrid search: keyword matching + semantic similarity
- Tiered filtering to reduce false positives on legal terms
- LLM (Groq/Anthropic) synthesizes legislative intent from debate chunks

**API Layer (`src/api/`)**

- FastAPI with routes for laws, articles, search, and CGI article analysis
- Streaming support for long-running analyses (`/analysis/law/stream`)

## Key Files

| File                                | Purpose                                                                              |
| ----------------------------------- | ------------------------------------------------------------------------------------ |
| `src/db/models.py`                  | SQLAlchemy models: lois → articles → dossiers → debats → interventions → amendements |
| `src/utils/config.py`               | Pydantic settings loaded from `.env`                                                 |
| `src/indexing/search.py`            | SearchService with hybrid search and keyword expansion                               |
| `src/analysis/article_analyzer.py`  | Multi-stage article analysis orchestration                                           |
| `scripts/run_full_pipeline.py`      | Main pipeline with all 7 phases                                                      |
| `data/lois_finances_2012_2025.json` | Reference list of 35 finance laws                                                    |
| `data/article_modifications.json`   | Pre-computed article modification index                                              |

## Configuration

Copy `.env.example` to `.env`:

```bash
# Required for data collection
PISTE_CLIENT_ID=xxx          # Légifrance OAuth (piste.gouv.fr)
PISTE_CLIENT_SECRET=xxx
PISTE_SANDBOX=true           # false for production API

# Database
DATABASE_URL=postgresql://localhost/intention_backup

# Embedding provider: "local" (free) or "openai"
EMBEDDING_PROVIDER=local
LOCAL_EMBEDDING_MODEL=paraphrase-multilingual-MiniLM-L12-v2

# LLM provider: "groq" (free tier) or "anthropic"
LLM_PROVIDER=groq
GROQ_API_KEYS=gsk_key1,gsk_key2,gsk_key3          # (recommended): Comma-separated list of keys
GROQ_API_KEY=your_groq_api_key                    # (legacy): Single key fallback
```

## Development Notes

### Hybrid Search Strategy

The search system uses tiered keyword filtering to reduce false positives:

1. **Tier 1 (strict)**: Exact article reference matches (e.g., "150-0 B ter")
2. **Tier 2 (relaxed)**: Broader keyword matches requiring article context

Generic terms like "plus-value", "cession", "holding" only match when accompanied by article-specific context.

### Embedding Checkpoint System

Long embedding generation supports checkpoints to resume after interruption:

- Checkpoints saved to `data/embeddings/{store}_checkpoint.npz`
- Automatically resumed on restart
- Deleted after successful completion

### Article Modification Index

Pre-computed article modification history in `data/article_modifications.json` includes:

- Curated quotes from parliamentary debates
- Synthesis evolution tables
- Forward-looking PLF discussions

## Language Conventions

- **Code**: English
- **Data/Content**: French (legal texts, debates)
- **User-facing UI**: French
