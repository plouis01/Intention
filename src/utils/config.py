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
    # Provider: "local" (free, sentence-transformers), "openai" (requires OPENAI_API_KEY),
    # or "vertex" (Google Vertex AI, requires gcloud auth)
    embedding_provider: str = "local"

    # OpenAI API for embeddings (when embedding_provider="openai")
    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 1536  # Can be 256, 1024, or 3072 for text-embedding-3-large

    # Local embedding model (when embedding_provider="local")
    # Fast (CPU): paraphrase-multilingual-MiniLM-L12-v2 (384 dim)
    # Best quality (GPU): OrdalieTech/Solon-embeddings-large-0.1 (1024 dim, French legal domain)
    local_embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"

    # Vertex AI embeddings (when embedding_provider="vertex")
    # Uses Google Cloud text-embedding-005 (768 dim)
    # Auth: gcloud auth application-default login
    vertex_project: str = ""
    vertex_location: str = "us-central1"
    vertex_embedding_model: str = "text-embedding-005"

    # LLM configuration for intent extraction
    # Provider: "anthropic" (requires ANTHROPIC_API_KEY) or "groq" (free, requires GROQ_API_KEY)
    llm_provider: str = "groq"

    # Anthropic API for intent extraction (when llm_provider="anthropic")
    anthropic_api_key: str = ""
    claude_model: str = "claude-haiku-4-5-20251001"
    anthropic_call_delay: float = 1.0  # Min seconds between Anthropic API calls (SDK handles retry)

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
    # French-native CamemBERTv2 reranker - scores in 0-1 range (not raw logits)
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
    # Tuned for text-embedding-005 (768d) which produces more discriminant scores
    search_min_similarity: float = 0.30
    search_keyword_boost: float = 0.35
    # Cross-law reference detection: minimum chunk score for regex detection
    cross_ref_min_score: float = 0.35
    # Cross-law reference detection: enable Groq-based implicit reference detection
    cross_ref_enable_llm: bool = False
    search_tier2_min_required: int = 2
    reranker_min_score: float = 0.1
    quote_word_overlap_threshold: float = 0.60
    quote_min_length: int = 40
    chamber_balance_min_per_chamber: int = 3
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
    # Tuned for text-embedding-004 (raised from 0.15)
    multi_stage_min_similarity: float = 0.20

    # Bicameral search pipeline: separate AN/Sénat searches with per-modification granularity
    bicameral_search_enabled: bool = True
    # Date window: "dossier" = tight (actual debate dates ± 7j), "fixed" = legacy 450j
    date_window_mode: str = "dossier"
    # Use articles_mentionnes JSON field as primary filter before ILIKE fallback
    articles_mentionnes_filter: bool = True
    # Minimum quality threshold: chamber included only if ≥1 chunk has explicit article ref
    bicameral_quality_threshold: int = 1
    # Sibling competition: ratio threshold to assign a chunk to a specific modification
    sibling_competition_ratio: float = 1.5

    # Multi-signal relevance scoring weights (must sum to ~1.0)
    signal_weight_semantic: float = 0.20
    signal_weight_article_ref: float = 0.20
    signal_weight_keywords: float = 0.15
    signal_weight_speaker: float = 0.15
    signal_weight_etape: float = 0.05
    signal_weight_temporal: float = 0.05
    signal_weight_amendment: float = 0.10
    signal_weight_crossencoder: float = 0.10

    # Auth / JWT
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 30

    # OAuth providers
    google_client_id: str = ""
    # Resend (email)
    resend_api_key: str = ""
    frontend_url: str = "http://localhost:5173"

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_pro_monthly_price_id: str = ""
    stripe_pro_yearly_price_id: str = ""

    # Trial
    trial_duration_days: int = 7
    trial_link_validity_days: int = 30  # How long a link stays valid before first open

    # Admin
    admin_emails: str = ""  # comma-separated emails that get admin role on signup

    def get_admin_emails(self) -> list[str]:
        return [e.strip() for e in self.admin_emails.split(",") if e.strip()]

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
