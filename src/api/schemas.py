"""Pydantic schemas for API request/response models."""

from pydantic import BaseModel, Field

# ============================================================================
# Health/Status Schemas
# ============================================================================


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "ok"
    version: str = "0.2.0"
    embedding_provider: str
    llm_provider: str
    database_status: str
    debate_chunks_count: int = 0
    amendment_chunks_count: int = 0


class ErrorResponse(BaseModel):
    """Error response."""

    error: str
    detail: str | None = None


# ============================================================================
# Analysis Schemas
# ============================================================================


class AvailableLawResponse(BaseModel):
    """An available law for analysis."""

    numero: str
    type: str
    annee_budgetaire: int
    titre: str = Field(default="")
    date_promulgation: str = Field(default="")


class AnalysisNotFoundResponse(BaseModel):
    """Response when no persisted analysis exists."""

    status: str = "not_found"
    message: str


# ============================================================================
# CGI Article Schemas
# ============================================================================


class CGIModification(BaseModel):
    """A modification to a CGI article."""

    date: str = Field(..., description="Date of modification (ISO format)")
    law_numero: str = Field(..., description="Law number (e.g., '2023-1322')")
    law_type: str = Field(..., description="Type of law (LF, LFR, LFSS)")
    law_year: int = Field(..., description="Budget year")
    law_title: str = Field(default="", description="Full law title")
    article_in_law: str = Field(..., description="Article reference in the law")
    nature: str = Field(..., description="Nature of modification (Création, Modification, Abrogation)")
    legifrance_url: str = Field(default="", description="Légifrance URL")


class CGIArticleSummary(BaseModel):
    """Summary of a CGI article (for listing)."""

    reference: str = Field(..., description="Article reference (e.g., '150-0 B ter')")
    titre: str = Field(default="", description="Article title/description")
    code: str = Field(default="CGI", description="Code (always CGI)")
    modification_count: int = Field(default=0, description="Number of modifications")
    last_modified: str | None = Field(default=None, description="Date of last modification")


class CGIArticleDetail(BaseModel):
    """Detailed CGI article information."""

    reference: str = Field(..., description="Article reference (e.g., '150-0 B ter')")
    titre: str = Field(default="", description="Article title/description")
    code: str = Field(default="CGI", description="Code (always CGI)")
    legifrance_url: str = Field(default="", description="Légifrance URL")
    etat_actuel: str = Field(default="", description="Current state (VIGUEUR, ABROGE, etc.)")
    keywords: list[str] = Field(default_factory=list, description="Search keywords")
    modifications: list[CGIModification] = Field(default_factory=list, description="Modification history")


class CGIArticleListResponse(BaseModel):
    """Response for listing CGI articles."""

    total: int
    articles: list[CGIArticleSummary]
