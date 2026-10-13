# Architecture Review: Intention du Législateur

## Project Goal

Web application where users (French tax lawyers) can:

1. Ask questions about legislative intent behind CGI tax law articles.
2. Get per-article analysis with quotes from parliamentary debates (AN + Sénat).
3. See the full modification history of each CGI article across 35 finance laws (2012-2025).

Frontend is in a separate repo; this repo is the **data pipeline + API backend**.

## Current Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                    Frontend (separate repo)                          │
└────────────────────────────┬─────────────────────────────────────────┘
                             │ NDJSON streaming / JSON
┌────────────────────────────▼─────────────────────────────────────────┐
│                   FastAPI Backend  (src/api/)                        │
│                                                                      │
│  cgi_routes.py                 routes.py                             │
│  ┌───────────────────────┐     ┌──────────────────────────────┐      │
│  │ GET /cgi              │     │ GET  /health                 │      │
│  │ GET /cgi/{ref}        │     │ GET  /analysis/laws          │      │
│  │ GET /cgi/{ref}/       │     └──────────────────────────────┘      │
│  │     analysis/stream   │                                           │
│  │ GET /cgi/{ref}/       │     main.py                               │
│  │     analysis          │     ┌──────────────────────────────┐      │
│  │ DELETE /cgi/{ref}/    │     │ Lifespan: init DB, preload   │      │
│  │     cache             │     │ stores, validate embedding   │      │
│  └───────────────────────┘     │ dimensions at startup        │      │
│                                └──────────────────────────────┘      │
└────────────────────────────┬─────────────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────────────┐
│                       Service Layer                                  │
│                                                                      │
│  ┌──────────────────┐  ┌───────────────────┐  ┌──────────────────┐   │
│  │ SearchService    │  │ ArticleAnalyzer   │  │ ArticleIndex     │   │
│  │ (search.py)      │  │ (article_         │  │ (article_        │   │
│  │                  │  │  analyzer.py)     │  │  index.py)       │   │
│  │ - hybrid_search  │  │                   │  │                  │   │
│  │ - search_cgi_    │  │ - analyze_article │  │ - get_article    │   │
│  │   article        │  │ - _analyze_       │  │ - get_curated_   │   │
│  │ - keyword_search │  │   modification    │  │   quotes         │   │
│  │ - search_text    │  │ - _call_llm       │  │ - get_synthese   │   │
│  │ - BM25 fallback  │  │ - _augment_with_  │  │ - get_           │   │
│  │ - pgvector ILIKE │  │   curated_quotes  │  │   jurisprudence  │   │
│  │   fallback       │  │ - _build_         │  │ - _resolve_ref   │   │
│  └────────┬─────────┘  │   synthesis       │  │   (case-         │   │
│           │            └────────┬──────────┘  │   insensitive)   │   │
│           │                     │             └────────┬─────────┘   │
│           │            ┌────────▼──────────┐           │             │
│           │            │ GroqClientPool    │    ┌──────▼─────────┐   │
│           │            │ (groq_pool.py)    │    │ article_       │   │
│           │            │ - key rotation    │    │ modifications  │   │
│           │            │ - Anthropic       │    │ .json          │   │
│           │            │   fallback        │    │ (4,403 arts)   │   │
│           │            │ - placeholder key │    └────────────────┘   │
│           │            │   detection       │                         │
│           │            └──────────────────┘                          │
└───────────┼──────────────────────────────────────────────────────────┘
            │
┌───────────▼──────────────────────────────────────────────────────────┐
│                         Data Layer                                    │
│                                                                      │
│  ┌──────────────────────┐  ┌──────────────────────────────────────┐   │
│  │ PostgreSQL (prod)    │  │ pgvector on chunk_embeddings (prod)  │   │
│  │ SQLite (dev)         │  │ In-memory stores (dev)               │   │
│  │                      │  │                                      │   │
│  │ Tables:              │  │ - ~574K debate chunks                │   │
│  │  - lois              │  │ - ~50K amendment chunks              │   │
│  │  - articles          │  │ - HNSW index for cosine similarity  │   │
│  │  - dossiers_         │  │ - ILIKE text_only_search fallback   │   │
│  │    legislatifs       │  │                                      │   │
│  │  - debats            │  │ Also supports:                       │   │
│  │  - interventions     │  │  - Qdrant (large-scale)              │   │
│  │  - amendements       │  │  - Database store (SQLite dev)       │   │
│  │  - sources_externes  │  └──────────────────────────────────────┘   │
│  │  - analysis_cache    │                                             │
│  │  - chunk_embeddings  │  ┌──────────────────────────────────────┐   │
│  └──────────────────────┘  │ JSON Files                           │   │
│                            │  - article_modifications.json        │   │
│                            │  - lois_finances_2012_2025.json      │   │
│                            │  - legal_term_expansions.json        │   │
│                            └──────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────┘
```

## Pipeline (7 phases)

```bash
python scripts/run_full_pipeline.py --all
```

```
COLLECT → IMPORT → LINK → FIX → EMBED → INDEX → VALIDATE
   │         │        │      │      │       │        │
   │         │        │      │      │       │        └─ Record counts, completeness
   │         │        │      │      │       └─ Build article_modifications.json
   │         │        │      │      └─ Generate embeddings (9h+, checkpointed)
   │         │        │      └─ Fix encoding in interventions/sources
   │         │        └─ Debate↔dossier linking, amendment↔article linking
   │         └─ Core import + DILA debates + Sénat + external sources
   └─ Légifrance API + AN open data + Sénat archives + DILA
```

## What's Working

### Data Collection

- Collectors for Légifrance (OAuth), AN, Sénat, DILA archives
- 35 finance laws (2012-2025), 760K+ interventions, 69K+ amendments
- Sources externes: rapports, jurisprudence, BOFiP

### Analysis Pipeline

- Multi-stage per-modification analysis: hybrid search → LLM extraction → curated quote augmentation
- Cross-section dedup for curated quotes
- Consolidation period detection, synthesis, forward-looking sections
- Exposé des motifs pulled from `sources_externes` table

### Search

- Hybrid: semantic (cosine similarity) + keyword (BM25 / ILIKE fallback)
- Tiered keyword filtering to reduce false positives on legal terms
- Legal term expansion (`legal_term_expansions.json`)
- pgvector ILIKE fallback when BM25 index unavailable

### API

- Streaming NDJSON endpoint (`/cgi/{ref}/analysis/stream`) - primary user-facing
- Cached analysis with hash-based invalidation and TTL
- Auto-reload of `article_modifications.json` on file change
- Thread-safe service initialization, session management
- Embedding dimension validation at startup (fail-fast on mismatch)

### LLM Integration

- GroqClientPool: multi-key rotation, cooldown, exponential backoff
- Anthropic fallback (with placeholder key detection)
- 90s timeout on all LLM calls
- Permanent error detection (403/forbidden → skip retry)

## Data Quality: `article_modifications.json`

**Overall grade: C (5.5/10)**

| Metric                                             | Value                                                               |
| -------------------------------------------------- | ------------------------------------------------------------------- |
| Total articles                                     | 4,403                                                               |
| Articles with curated quotes                       | 2,562 (58.2%)                                                       |
| Total curated quotes                               | 6,015                                                               |
| Articles with 5+ mods and zero quotes              | **111** (critical gap)                                              |
| Year gaps (articles with quotes but missing years) | 89.2%                                                               |
| Quote sources                                      | 74.7% legacy (no source field), 23.1% `topic_search`, 2.0% `direct` |
| Source-verified quotes                             | 8.4%                                                                |
| Synthese/synthese_evolution coverage               | 100%                                                                |
| Jurisprudence coverage                             | 3.2% (139 articles)                                                 |
| Forward-looking coverage                           | 7.3% (323 articles)                                                 |

### Key article coverage

| Article      | Domain         | Mods | Quotes | Year coverage | Grade |
| ------------ | -------------- | ---- | ------ | ------------- | ----- |
| 150-0 B ter  | Apport-cession | 13   | 9      | 4/8           | A-    |
| 39           | BIC profit     | 67   | 29     | 16/24         | B+    |
| 209          | IS base        | 34   | 23     | 10/16         | B     |
| 244 quater B | CIR            | 39   | 23     | 12/20         | B     |
| 219          | IS rates       | 31   | 16     | 8/14          | B-    |
| 197          | IR brackets    | 48   | 5      | 5/24          | D     |
| 1            | IR principles  | 51   | 3      | 3/24          | D-    |
| 5            | Non-residents  | 42   | 1      | 1/22          | F     |
| 40           | Capital gains  | 40   | 2      | 1/22          | F     |

### Data hygiene issues

- `chambre` casing inconsistency: `SENAT` (2,118) vs `Senat` (1,065)
- 407 quotes with empty `fonction` field
- 12 quotes with `rapporteurM` concatenation artifact
- HTML entities in `fonction` values (`pr&#233;sident`)

## Current API Endpoints

| Endpoint                     | Method | Description                                   |
| ---------------------------- | ------ | --------------------------------------------- |
| `/cgi`                       | GET    | List all CGI articles (paginated, searchable) |
| `/cgi/{ref}`                 | GET    | Article detail + modification history         |
| `/cgi/{ref}/analysis/stream` | GET    | Streaming NDJSON analysis (primary endpoint)  |
| `/cgi/{ref}/analysis`        | GET    | Cached analysis (JSON)                        |
| `/cgi/{ref}/cache`           | DELETE | Invalidate cached analysis                    |
| `/analysis/laws`             | GET    | List 35 finance laws                          |
| `/health`                    | GET    | Service status, DB, embedding counts          |

## Database Schema (actual)

### `analysis_cache` (implemented)

```sql
CREATE TABLE analysis_cache (
    id SERIAL PRIMARY KEY,
    cgi_article VARCHAR(50) NOT NULL,
    analysis_json JSON NOT NULL,          -- full consolidated analysis
    modifications_hash VARCHAR(64),        -- SHA-256 for invalidation
    modification_count INTEGER DEFAULT 0,
    last_modification_date VARCHAR(10),
    quality_score INTEGER,                 -- 0-100
    sources_count INTEGER DEFAULT 0,
    confidence_level VARCHAR(20),          -- élevé, moyen, faible
    created_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP,
    embedding_model VARCHAR(100),
    llm_model VARCHAR(100)
);
```

### `chunk_embeddings` (pgvector production)

```sql
CREATE TABLE chunk_embeddings (
    id SERIAL PRIMARY KEY,
    chunk_type VARCHAR(50),               -- 'intervention' or 'amendement'
    content TEXT NOT NULL,
    chunk_metadata JSON,
    source_id VARCHAR(100),
    position INTEGER,
    embedding vector(384) NOT NULL,       -- pgvector column (MiniLM = 384d)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- HNSW index for cosine similarity
CREATE INDEX ON chunk_embeddings USING hnsw (embedding vector_cosine_ops);
```

## Recent Bug Fixes (this session)

| ID    | Fix                                                       | File                  |
| ----- | --------------------------------------------------------- | --------------------- |
| B1    | Curated quote augmentation in streaming endpoint          | `cgi_routes.py`       |
| B2+B3 | Embedding dimension validation at startup                 | `main.py`             |
| B4    | pgvector ILIKE fallback for keyword_search                | `search.py`           |
| B5    | Placeholder Anthropic API key detection                   | `groq_pool.py`        |
| B6    | Race condition in `save_cached_analysis` (IntegrityError) | `cgi_routes.py`       |
| B7    | Thread-safe `get_search_service` (double-checked locking) | `routes.py`           |
| B8    | Session leak in health check                              | `routes.py`           |
| Q1    | 90s timeout on LLM calls                                  | `article_analyzer.py` |
| Q2    | Cross-section curated quote dedup in streaming            | `cgi_routes.py`       |
| Q3    | Auto-reload article index on file change                  | `cgi_routes.py`       |
| Q4    | SQL-level filtering in `_fetch_expose_motifs`             | `article_analyzer.py` |
| M1    | Skip retry on permanent LLM errors (403)                  | `article_analyzer.py` |
| M2    | UTC timezone for `generated_date`                         | `article_analyzer.py` |
| M3    | Log embedding errors in health check                      | `routes.py`           |
| -     | Case-insensitive lookups in ArticleIndex                  | `article_index.py`    |

## Remaining Gaps

### Priority 1: Data Quality

- **Fill 111 zero-quote articles with 5+ mods** - these produce empty analyses
- **Fill year gaps on top 30 articles** - articles 5, 40, 1, 197 are near-empty
- **Clean data hygiene issues** - chambre casing, empty fonctions, HTML entities

### Priority 2: Production Hardening

- Rate limiting middleware (per-IP for anonymous, higher for authenticated)
- Request logging and monitoring
- Error tracking (Sentry or equivalent)
- Authentication for write endpoints (cache invalidation)

### Priority 3: Features

- User feedback table (`user_feedback` with rating + comment)
- Batch pre-generation of analyses for top articles
- Webhook or scheduled job to re-analyze when `article_modifications.json` changes
