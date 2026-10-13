"""Configuration management using pydantic-settings."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Légifrance API (PISTE)
    piste_client_id: str = ""
    piste_client_secret: str = ""
    piste_sandbox: bool = True  # Set via PISTE_SANDBOX env var

    @property
    def piste_token_url(self) -> str:
        if self.piste_sandbox:
            return "https://sandbox-oauth.piste.gouv.fr/api/oauth/token"
        return "https://oauth.piste.gouv.fr/api/oauth/token"

    @property
    def piste_api_base(self) -> str:
        if self.piste_sandbox:
            return "https://sandbox-api.piste.gouv.fr/dila/legifrance/lf-engine-app"
        return "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"

    # Database
    database_url: str = "sqlite:///data/legislateur.db"

    @property
    def effective_database_url(self) -> str:
        """Return database URL with scheme fix for SQLAlchemy 2.x compatibility.

        Railway provides postgres:// URLs but SQLAlchemy 2.x requires postgresql://.
        """
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url

    # Embedding configuration
    # Provider: "openai" (requires OPENAI_API_KEY) or "local" (free, uses sentence-transformers)
    embedding_provider: str = "local"

    # OpenAI API for embeddings (when embedding_provider="openai")
    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 1536  # Can be 256, 1024, or 3072 for text-embedding-3-large

    # Local embedding model (when embedding_provider="local")
    # Fast (CPU): paraphrase-multilingual-MiniLM-L12-v2 (384 dim)
    # Best quality (GPU): OrdalieTech/Solon-embeddings-large-0.1 (1024 dim, French legal domain)
    local_embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"

    # LLM configuration for intent extraction
    # Provider: "anthropic" (requires ANTHROPIC_API_KEY) or "groq" (free, requires GROQ_API_KEY)
    llm_provider: str = "groq"

    # Anthropic API for intent extraction (when llm_provider="anthropic")
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-20250514"

    # Groq API for intent extraction (when llm_provider="groq")
    # Free tier available at https://console.groq.com/
    # Supports multiple keys for rotation via comma-separated GROQ_API_KEYS
    # Example: GROQ_API_KEYS=gsk_xxx,gsk_yyy,gsk_zzz
    # Legacy single key GROQ_API_KEY is also supported for backwards compatibility
    groq_api_keys: str = ""  # Comma-separated list of API keys
    groq_api_key: str = ""  # Legacy single key (fallback)
    groq_model: str = "llama-3.3-70b-versatile"  # or mixtral-8x7b-32768

    def get_groq_api_keys(self) -> list[str]:
        """Get all configured Groq API keys for rotation."""
        keys = []
        # First, add keys from comma-separated list
        if self.groq_api_keys:
            keys.extend(k.strip() for k in self.groq_api_keys.split(",") if k.strip())
        # Fallback to legacy single key if no keys from list
        if not keys and self.groq_api_key:
            keys.append(self.groq_api_key)
        return keys

    # BM25 sparse retrieval
    bm25_enabled: bool = True

    # Extraction provider for article analysis
    # "llm" (default, uses Groq/Anthropic) or "langextract" (Google's source-grounded extraction)
    extraction_provider: str = "llm"

    # LangExtract settings (when extraction_provider="langextract")
    langextract_api_key: str = ""  # Google AI API key for Gemini
    langextract_model: str = "gemini-2.5-flash"

    # Reranker configuration
    # Cross-encoder model for rescoring search results after initial retrieval
    # French-native CamemBERTv2 reranker — scores in 0-1 range (not raw logits)
    reranker_enabled: bool = True
    reranker_model: str = "tomaarsen/reranker-camembertv2-base-fr"

    # Vector store configuration
    # Provider: "memory" (default), "database", "pgvector", "qdrant"
    vector_store_provider: str = "memory"

    # Qdrant configuration (when vector_store_provider="qdrant")
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection_debates: str = "debates"
    qdrant_collection_amendments: str = "amendments"

    # pgvector uses DATABASE_URL (PostgreSQL with pgvector extension)

    # Search thresholds (tunable via env or eval framework)
    search_min_similarity: float = 0.25
    search_keyword_boost: float = 0.5
    search_tier2_min_required: int = 3
    reranker_min_score: float = 0.1
    quote_word_overlap_threshold: float = 0.75
    quote_min_length: int = 80
    curated_augmentation_min_sources: int = 4
    speaker_boost_rapporteur_general: float = 0.08
    speaker_boost_rapporteur: float = 0.06
    speaker_boost_ministre: float = 0.05
    speaker_boost_commission_pres: float = 0.04

    # Extraction mode: "single" (default) or "two_step"
    extraction_mode: str = "single"

    # Multi-stage retrieval: when True, adds an LLM-based relevance filter
    # between search and synthesis. Stage 1: broad recall (lower thresholds),
    # Stage 2: LLM asks "Is this chunk about article X?", Stage 3: synthesis.
    # Requires LLM API access (Groq or Anthropic). Off by default.
    multi_stage_retrieval: bool = False
    # Lower similarity for Stage 1 broad recall (only used when multi_stage_retrieval=True)
    multi_stage_min_similarity: float = 0.15

    # CORS
    cors_origins: str = "http://localhost:5173"

    def get_cors_origins(self) -> list[str]:
        """Get list of allowed CORS origins."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # Environment
    environment: str = "development"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
