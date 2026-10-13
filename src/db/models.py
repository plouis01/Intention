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
    type = Column(String(20), nullable=False)  # LF, LFR, LFSS, REGLEMENT_CE, REGLEMENT_CEE
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
        Index("idx_amendements_dossier_numero", "dossier_id", "numero"),
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
    content_hash = Column(String(64), nullable=False, unique=True)  # SHA256 hash for deduplication
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
        Index("idx_chunk_embeddings_amendement_id", "amendement_id"),
        Index("idx_chunk_embeddings_intervention_id", "intervention_id"),
    )


class AnalysisCache(Base):
    """Cached LLM analysis results for CGI articles (one entry per article)."""

    __tablename__ = "analysis_cache"

    id = Column(Integer, primary_key=True)
    cgi_article = Column(String(50), nullable=False)  # e.g., "150-0 B ter"

    # Analysis result (JSON) — full consolidated analysis
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
    )


_engine = None
_SessionLocal = None


def get_engine():
    """Create database engine from settings (cached singleton)."""
    global _engine
    if _engine is None:
        settings = get_settings()
        is_pg = settings.effective_database_url.startswith("postgresql")
        kwargs = {"echo": settings.environment == "development"}
        if is_pg:
            kwargs["pool_pre_ping"] = True
            kwargs["pool_size"] = 5
            kwargs["max_overflow"] = 10
            kwargs["pool_timeout"] = 30
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


def init_db():
    """Initialize database tables."""
    engine = get_engine()
    Base.metadata.create_all(engine)


def reset_db():
    """Drop and recreate all tables (use with caution)."""
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
