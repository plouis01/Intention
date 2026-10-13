"""SQLAlchemy models for the legislative data schema."""

from enum import Enum

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

from src.utils.config import get_settings


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class User(Base):
    """User account for authentication."""

    __tablename__ = "users"

    id = Column(String(36), primary_key=True)  # UUID
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    hashed_password = Column(String(255), nullable=True)  # Nullable for OAuth-only users
    provider = Column(String(20), nullable=False, default="email")  # email, google, apple
    role = Column(String(20), nullable=False, server_default="user")  # user, admin
    provider_id = Column(String(255), nullable=True)  # OAuth subject ID
    email_verified = Column(Integer, nullable=False, server_default="0")  # 0=pending, 1=verified (SQLite compat)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=True)

    search_history = relationship("SearchHistory", back_populates="user", cascade="all, delete-orphan")
    trial_links = relationship("TrialLink", back_populates="user", foreign_keys="TrialLink.user_id")
    subscription = relationship("Subscription", back_populates="user", uselist=False)


class TrialLink(Base):
    """Trial access link created by admin for prospects."""

    __tablename__ = "trial_links"

    id = Column(Integer, primary_key=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    prospect_email = Column(String(255), nullable=False, index=True)
    prospect_name = Column(String(255), nullable=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    status = Column(String(20), nullable=False, server_default="pending")
    created_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=True)  # Set on first redemption (trial duration)
    link_expires_at = Column(DateTime, nullable=False)  # Link validity (before first open)
    redeemed_at = Column(DateTime, nullable=True)
    converted_at = Column(DateTime, nullable=True)
    visit_count = Column(Integer, nullable=False, server_default="0")
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)

    user = relationship("User", back_populates="trial_links", foreign_keys=[user_id])
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        Index("ix_trial_links_status", "status"),
    )


class TrialLinkVisit(Base):
    """Audit trail for every hit on a trial link (inspect or redeem).

    Lets admins distinguish real prospect opens from antivirus / email-scanner
    pre-fetches (Microsoft SafeLinks, Mimecast, Proofpoint, etc.).
    """

    __tablename__ = "trial_link_visits"

    id = Column(Integer, primary_key=True)
    trial_link_id = Column(Integer, ForeignKey("trial_links.id"), nullable=False, index=True)
    event_type = Column(String(20), nullable=False)  # "inspect" | "redeem"
    classification = Column(String(20), nullable=False)  # "human" | "bot" | "unknown"
    scanner_name = Column(String(50), nullable=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(300), nullable=True)
    accept_language = Column(String(100), nullable=True)
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_trial_visits_link_created", "trial_link_id", "created_at"),
    )


class Subscription(Base):
    """Stripe subscription for paid access."""

    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), unique=True, nullable=False)
    stripe_customer_id = Column(String(255), nullable=True)
    stripe_subscription_id = Column(String(255), nullable=True)
    plan = Column(String(20), nullable=False, server_default="pro")  # pro
    billing_period = Column(String(20), nullable=True)  # monthly, yearly
    status = Column(String(20), nullable=False, server_default="active")  # active, canceled, past_due, incomplete
    current_period_start = Column(DateTime, nullable=True)
    current_period_end = Column(DateTime, nullable=True)
    cancel_at_period_end = Column(Integer, nullable=False, server_default="0")  # 0/1, SQLite compat
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="subscription")

    __table_args__ = (
        Index("ix_subscriptions_user", "user_id", unique=True),
        Index("ix_subscriptions_status", "status"),
    )


class SearchHistory(Base):
    """Search history entry for a user."""

    __tablename__ = "search_history"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    article_ref = Column(String(100), nullable=False)
    article_title = Column(String(500), nullable=False)
    searched_at = Column(DateTime, nullable=False)

    user = relationship("User", back_populates="search_history")

    __table_args__ = (
        Index("ix_search_history_user_date", "user_id", "searched_at"),
    )


class Notification(Base):
    """In-app notifications for users."""

    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    link = Column(String(500), nullable=True)
    type = Column(String(50), nullable=True)  # "contact_reply", "article_added", etc.
    extra_data = Column("metadata", Text, nullable=True)  # JSON string for extra data
    read = Column(Integer, nullable=False, server_default="0")  # 0=unread, 1=read
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_notifications_user_read", "user_id", "read"),
    )


class UserSession(Base):
    """Active user sessions (one per device/browser)."""

    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    refresh_token_hash = Column(String(64), unique=True, nullable=False)  # SHA-256
    device_info = Column(String(200), nullable=True)  # User-Agent truncated
    ip_address = Column(String(45), nullable=True)  # IPv4 or IPv6
    location = Column(String(100), nullable=True)  # "City, Country"
    created_at = Column(DateTime, nullable=False)
    last_used_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_user_sessions_user", "user_id"),
        Index("ix_user_sessions_token_hash", "refresh_token_hash", unique=True),
        # Speeds up GET /auth/sessions which sorts by last_used_at DESC for a user.
        Index("ix_user_sessions_user_last_used", "user_id", "last_used_at"),
    )


class AccountLog(Base):
    """Anonymized account event log (GDPR-compliant, no personal data)."""

    __tablename__ = "account_logs"

    id = Column(Integer, primary_key=True)
    event = Column(String(50), nullable=False)  # account_created, account_deleted
    provider = Column(String(20), nullable=False)  # email, google
    timestamp = Column(DateTime, nullable=False)


class ContactMessage(Base):
    """Contact/support messages from users."""

    __tablename__ = "contact_messages"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    email = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, server_default="open")  # "open", "closed"
    last_seen_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False)


class ConversationMessage(Base):
    """Messages within a contact ticket thread."""

    __tablename__ = "conversation_messages"

    id = Column(Integer, primary_key=True)
    contact_id = Column(Integer, ForeignKey("contact_messages.id"), nullable=False)
    sender = Column(String(10), nullable=False)  # "user" or "admin"
    message = Column(Text, nullable=False)
    file_path = Column(String(500), nullable=True)
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_conversation_messages_contact", "contact_id"),
    )


class AnalysisFeedback(Base):
    """User feedback on article analyses."""

    __tablename__ = "analysis_feedback"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    article_ref = Column(String(100), nullable=False)
    section_index = Column(Integer, nullable=True)  # null = global, int = specific section
    rating = Column(String(10), nullable=True)  # up, down, or null (cleared)
    message = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False)

    admin_reply = Column(Text, nullable=True)
    admin_reply_at = Column(DateTime, nullable=True)
    admin_reply_by = Column(String(36), ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        Index("ix_analysis_feedback_article", "article_ref"),
    )


class ArticleReport(Base):
    """User-reported missing articles."""

    __tablename__ = "article_reports"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    article_ref = Column(String(100), nullable=False)
    notify = Column(Integer, default=0)  # 1 = user wants email notification
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_article_reports_ref", "article_ref"),
    )


class PasswordResetToken(Base):
    """Token for password reset requests."""

    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    token = Column(String(36), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Integer, default=0)  # 0=unused, 1=used (SQLite compat)


class EmailVerificationToken(Base):
    """Single-use token to verify a newly-signed-up email address."""

    __tablename__ = "email_verification_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    token = Column(String(36), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Integer, default=0)


class EmailChangeToken(Base):
    """Token emitted when a user requests an email change; confirmed via the new email."""

    __tablename__ = "email_change_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    new_email = Column(String(255), nullable=False)
    token = Column(String(36), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Integer, default=0)


class AuthEvent(Base):
    """Structured audit log for authentication and profile events.

    Rows expire after 90 days (purge task at startup).
    user_id is nullable so we can record failed logins where no user matched.
    """

    __tablename__ = "auth_events"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    event = Column(String(50), nullable=False, index=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(200), nullable=True)
    extra_data = Column(Text, nullable=True)  # JSON string
    created_at = Column(DateTime, nullable=False, index=True)


class LoiType(str, Enum):
    """Types of finance laws."""

    LF = "LF"  # Loi de Finances initiale
    LFR = "LFR"  # Loi de Finances Rectificative
    LFSS = "LFSS"  # Loi de Financement de la Sécurité Sociale


class Chambre(str, Enum):
    """Parliamentary chambers."""

    AN = "AN"  # Assemblée Nationale
    SENAT = "SENAT"  # Sénat


class EtapeType(str, Enum):
    """Legislative process stages."""

    COMMISSION = "commission"
    SEANCE_PUBLIQUE = "seance_publique"


class FonctionOrateur(str, Enum):
    """Speaker roles in debates."""

    RAPPORTEUR = "rapporteur"
    MINISTRE = "ministre"
    DEPUTE = "depute"
    SENATEUR = "senateur"
    PRESIDENT = "president"
    AUTRE = "autre"


class SortAmendement(str, Enum):
    """Amendment outcomes."""

    ADOPTE = "adopte"
    REJETE = "rejete"
    RETIRE = "retire"
    TOMBE = "tombe"
    NON_SOUTENU = "non_soutenu"


class Loi(Base):
    """Finance law (LF, LFR, LFSS)."""

    __tablename__ = "lois"

    id = Column(Integer, primary_key=True)
    numero = Column(String(20), unique=True, nullable=False)  # e.g., "2023-1322"
    type = Column(String(10), nullable=False)  # LF, LFR, LFSS
    annee = Column(Integer, nullable=False)
    date_promulgation = Column(DateTime, nullable=False)
    titre = Column(String(500), nullable=False)
    nor = Column(String(20))  # NOR code for Légifrance
    url_legifrance = Column(String(500))

    # Relationships
    articles = relationship("Article", back_populates="loi", cascade="all, delete-orphan")
    dossiers = relationship(
        "DossierLegislatif", back_populates="loi", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_lois_annee_type", "annee", "type"),)


class Article(Base):
    """Individual article within a law."""

    __tablename__ = "articles"

    id = Column(Integer, primary_key=True)
    loi_id = Column(Integer, ForeignKey("lois.id"), nullable=False)
    numero_article = Column(String(50), nullable=False)  # e.g., "1", "1 bis", "12 A"
    contenu_texte = Column(Text, nullable=False)
    position_dans_loi = Column(Integer)  # Order in the law

    # Relationships
    loi = relationship("Loi", back_populates="articles")
    amendements = relationship("Amendement", back_populates="article")

    __table_args__ = (Index("ix_articles_loi_numero", "loi_id", "numero_article"),)


class DossierLegislatif(Base):
    """Legislative dossier from AN or Sénat."""

    __tablename__ = "dossiers_legislatifs"

    id = Column(Integer, primary_key=True)
    loi_id = Column(Integer, ForeignKey("lois.id"), nullable=False)
    chambre = Column(String(10), nullable=False)  # AN, SENAT
    url_source = Column(String(500))
    reference_externe = Column(String(100))  # External ID from source
    etapes = Column(JSON)  # List of stages: ["1ere lecture", "2eme lecture", "CMP"]

    # Relationships
    loi = relationship("Loi", back_populates="dossiers")
    debats = relationship("Debat", back_populates="dossier", cascade="all, delete-orphan")
    amendements = relationship(
        "Amendement", back_populates="dossier", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_dossiers_loi_chambre", "loi_id", "chambre"),)


class Debat(Base):
    """Debate session (commission or public session)."""

    __tablename__ = "debats"

    id = Column(Integer, primary_key=True)
    dossier_id = Column(Integer, ForeignKey("dossiers_legislatifs.id"), nullable=True)
    chambre = Column(String(10), nullable=False)
    date = Column(DateTime, nullable=False)
    etape = Column(String(50))  # commission, seance_publique
    contenu_integral = Column(Text)  # Full debate text
    url_source = Column(String(500))
    reference_externe = Column(String(100))

    # Relationships
    dossier = relationship("DossierLegislatif", back_populates="debats")
    interventions = relationship(
        "Intervention", back_populates="debat", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_debats_dossier_date", "dossier_id", "date"),
        Index("ix_debats_reference_externe", "reference_externe", unique=True),
    )


class Intervention(Base):
    """Individual speaker intervention within a debate."""

    __tablename__ = "interventions"

    id = Column(Integer, primary_key=True)
    debat_id = Column(Integer, ForeignKey("debats.id"), nullable=False)
    orateur = Column(Text, nullable=False)
    fonction = Column(Text)  # rapporteur, ministre, depute, senateur
    contenu = Column(Text, nullable=False)
    articles_mentionnes = Column(JSON)  # List of article numbers mentioned
    timestamp_debut = Column(DateTime)
    ordre = Column(Integer)  # Order within debate

    # Relationships
    debat = relationship("Debat", back_populates="interventions")

    __table_args__ = (Index("ix_interventions_debat_ordre", "debat_id", "ordre"),)


class Amendement(Base):
    """Amendment proposed during legislative process."""

    __tablename__ = "amendements"

    id = Column(Integer, primary_key=True)
    dossier_id = Column(Integer, ForeignKey("dossiers_legislatifs.id"), nullable=False)
    article_id = Column(Integer, ForeignKey("articles.id"))  # May be null if article not found
    numero = Column(String(50), nullable=False)  # Amendment number
    auteur = Column(String(500))  # Author(s)
    objet = Column(Text)  # Object/purpose
    contenu = Column(Text)  # Amendment content
    expose_motifs = Column(Text)  # Explanatory statement
    sort = Column(String(20))  # adopte, rejete, retire, tombe, non_soutenu
    date_depot = Column(DateTime)
    url_source = Column(String(500))
    reference_externe = Column(String(100))

    # Relationships
    dossier = relationship("DossierLegislatif", back_populates="amendements")
    article = relationship("Article", back_populates="amendements")

    __table_args__ = (
        Index("ix_amendements_dossier", "dossier_id"),
        Index("ix_amendements_article", "article_id"),
        Index("ix_amendements_sort", "sort"),
        Index("ix_amendements_reference_externe", "reference_externe", unique=True),
    )


class ChunkType(str, Enum):
    """Types of chunks for embeddings."""

    INTERVENTION = "intervention"
    ARTICLE_DISCUSSION = "article_discussion"
    AMENDEMENT = "amendement"
    PARAGRAPH = "paragraph"


class ChunkEmbedding(Base):
    """Stored chunk with its embedding vector for semantic search."""

    __tablename__ = "chunk_embeddings"

    id = Column(Integer, primary_key=True)
    chunk_type = Column(String(50), nullable=False)  # intervention, amendement, etc.
    content = Column(Text, nullable=False)  # The text that was embedded
    content_hash = Column(String(64), nullable=False)  # SHA256 hash (unique per source_id)
    chunk_metadata = Column(JSON)  # {orateur, fonction, chambre, date, etc.}
    source_id = Column(String(100))  # Reference to source (debat_id, amendement_id)
    position = Column(Integer)  # Order within source

    embedding_model = Column(String(100), nullable=False)  # Model used to generate embedding
    embedding_dim = Column(Integer, nullable=False)  # Dimension of embedding vector

    # Timestamps
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime)

    # Optional foreign keys for linking to source records
    intervention_id = Column(Integer, ForeignKey("interventions.id"), nullable=True)
    amendement_id = Column(Integer, ForeignKey("amendements.id"), nullable=True)

    __table_args__ = (
        Index("ix_chunk_embeddings_type", "chunk_type"),
        Index("ix_chunk_embeddings_source", "source_id"),
        Index("ix_chunk_embeddings_model", "embedding_model"),
    )


class AnalysisCache(Base):
    """Cached LLM analysis results for CGI articles (one entry per article)."""

    __tablename__ = "analysis_cache"

    id = Column(Integer, primary_key=True)
    cgi_article = Column(String(50), nullable=False)  # e.g., "150-0 B ter"

    # Analysis result (JSON) - full consolidated analysis
    analysis_json = Column(JSON, nullable=False)

    # Invalidation tracking
    modifications_hash = Column(String(64))  # SHA-256 of sorted modifications
    modification_count = Column(Integer, default=0)
    last_modification_date = Column(String(10))  # e.g., "2024-12-30"

    # Quality metrics
    quality_score = Column(Integer)  # 0-100
    sources_count = Column(Integer, default=0)
    confidence_level = Column(String(20))  # élevé, moyen, faible

    # Metadata
    created_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime)  # Optional expiration
    embedding_model = Column(String(100))  # Model used for search
    llm_model = Column(String(100))  # Model used for analysis

    __table_args__ = (
        Index("ix_analysis_cache_article", "cgi_article", unique=True),
        Index("ix_analysis_cache_expires", "expires_at"),
    )


class SourceExterneType(str, Enum):
    """Types of external sources."""

    RAPPORT = "rapport"  # Parliamentary report
    SEANCE = "seance"  # Floor debate session
    AMENDEMENT_SENAT = "amendement_senat"  # Sénat amendment
    BOFIP = "bofip"  # Tax doctrine
    JURISPRUDENCE = "jurisprudence"  # Court decisions
    CADF = "cadf"  # Comité de l'abus de droit fiscal
    AUTRE = "autre"


class SourceExterne(Base):
    """External source fetched from the web (not in primary database)."""

    __tablename__ = "sources_externes"

    id = Column(Integer, primary_key=True)
    type = Column(String(50), nullable=False)  # rapport, seance, amendement, bofip, jurisprudence
    titre = Column(String(500), nullable=False)
    url = Column(String(1000), nullable=False)
    date = Column(DateTime)
    chambre = Column(String(10))  # AN, SENAT, CE, CC
    reference = Column(String(200))  # e.g., "Rapport n° 213", "CE 472835"
    auteur = Column(String(500))
    contenu = Column(Text)  # Full or extracted content
    resume = Column(Text)  # Summary
    articles_cgi_lies = Column(JSON)  # Related CGI articles, e.g., ["150-0 B ter"]
    lois_liees = Column(JSON)  # Related laws, e.g., ["2023-1322"]
    date_collecte = Column(DateTime, nullable=False)
    extra_data = Column(JSON)  # Additional metadata

    __table_args__ = (
        Index("ix_sources_externes_type", "type"),
        Index("ix_sources_externes_type_ref", "type", "reference"),
        Index("ix_sources_externes_type_date", "type", "date"),
    )


_engine = None
_SessionLocal = None
_engine_light = None
_SessionLocal_light = None


def get_engine():
    """Heavy pool: used by analyses, search, background workers."""
    global _engine
    if _engine is None:
        settings = get_settings()
        is_pg = settings.effective_database_url.startswith("postgresql")
        kwargs = {"echo": settings.environment == "development"}
        if is_pg:
            kwargs["pool_pre_ping"] = True
            kwargs["pool_size"] = 15
            kwargs["max_overflow"] = 25
            kwargs["pool_timeout"] = 5
            kwargs["pool_recycle"] = 600
        _engine = create_engine(settings.effective_database_url, **kwargs)
        _init_sessionmaker()
    return _engine


def _init_sessionmaker():
    """Initialize the global sessionmaker bound to the engine."""
    global _SessionLocal
    _SessionLocal = sessionmaker(bind=_engine)


def get_session():
    """Create a new database session from the singleton sessionmaker."""
    if _SessionLocal is None:
        get_engine()
    return _SessionLocal()


def get_engine_light():
    """Light pool: dedicated to short-lived auth/trial/admin/subscription endpoints.
    Isolated from the heavy pool so analyses can never starve auth queries."""
    global _engine_light, _SessionLocal_light
    if _engine_light is None:
        settings = get_settings()
        is_pg = settings.effective_database_url.startswith("postgresql")
        kwargs = {"echo": settings.environment == "development"}
        if is_pg:
            kwargs["pool_pre_ping"] = True
            kwargs["pool_size"] = 8
            kwargs["max_overflow"] = 8
            kwargs["pool_timeout"] = 5
            kwargs["pool_recycle"] = 600
        _engine_light = create_engine(settings.effective_database_url, **kwargs)
        _SessionLocal_light = sessionmaker(bind=_engine_light)
    return _engine_light


def get_session_light():
    """Session on the LIGHT pool (auth/trial/admin/subscription only)."""
    if _SessionLocal_light is None:
        get_engine_light()
    return _SessionLocal_light()


def init_db():
    """Initialize database tables."""
    engine = get_engine()
    Base.metadata.create_all(engine)


def reset_db():
    """Drop and recreate all tables (use with caution).

    Safety rails: refuses to run against a PostgreSQL URL unless the URL or
    database name contains "test". SQLite is always allowed since it is
    developer-local and expected to be disposable. This prevents accidental
    data loss on shared / production databases (e.g. when a test fixture
    inadvertently picks up the developer's DATABASE_URL from .env).
    """
    settings = get_settings()
    url = settings.effective_database_url
    if url.startswith("postgresql"):
        # Only allow when the URL is clearly test-scoped
        if "test" not in url.lower():
            raise RuntimeError(
                "reset_db() refused: current DATABASE_URL does not look like a "
                f"test database ({url}). Override DATABASE_URL with a sqlite:// "
                "URL or a Postgres URL whose name contains 'test' before "
                "calling reset_db()."
            )
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
