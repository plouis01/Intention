"""Data models for article analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


class CertitudeLevel(str, Enum):
    """Level of certainty for intent interpretation."""

    ELEVE = "élevé"
    MOYEN = "moyen"
    FAIBLE = "faible"

    @classmethod
    def from_score(cls, score: float) -> "CertitudeLevel":
        """
        Convert a numeric confidence score (0.0-1.0) to CertitudeLevel.

        - >= 0.7: ELEVE (high)
        - >= 0.4: MOYEN (medium)
        - < 0.4: FAIBLE (low)
        """
        if score >= 0.7:
            return cls.ELEVE
        elif score >= 0.4:
            return cls.MOYEN
        else:
            return cls.FAIBLE

    @classmethod
    def from_sources_count(cls, count: int) -> "CertitudeLevel":
        """
        Convert a sources count to CertitudeLevel.

        - >= 3 sources: ELEVE
        - >= 1 source: MOYEN
        - 0 sources: FAIBLE
        """
        if count >= 3:
            return cls.ELEVE
        elif count >= 1:
            return cls.MOYEN
        else:
            return cls.FAIBLE

    @classmethod
    def from_weighted_sources(cls, sources: list["QuotedSource"]) -> "CertitudeLevel":
        """
        Convert sources to CertitudeLevel weighted by speaker importance.

        A single rapporteur or minister quote is more valuable than
        multiple generic député questions. Weights:
        - rapporteur général: 2.0
        - rapporteur: 1.5
        - ministre / secrétaire d'état: 1.5
        - président de commission: 1.0
        - député / sénateur / other: 0.5

        Thresholds: weighted_sum >= 2.0 → ELEVE, >= 0.5 → MOYEN
        """
        if not sources:
            return cls.FAIBLE

        weighted_sum = 0.0
        for source in sources:
            fonction = (source.fonction or "").lower()
            if "rapporteur" in fonction and ("général" in fonction or "genera" in fonction):
                weighted_sum += 2.0
            elif "rapporteur" in fonction:
                weighted_sum += 1.5
            elif "ministre" in fonction or "secrétaire d" in fonction:
                weighted_sum += 1.5
            elif "président" in fonction and "commission" in fonction:
                weighted_sum += 1.0
            else:
                weighted_sum += 0.5

        if weighted_sum >= 2.0:
            return cls.ELEVE
        elif weighted_sum >= 0.5:
            return cls.MOYEN
        else:
            return cls.FAIBLE

    @property
    def score(self) -> float:
        """Convert CertitudeLevel to a numeric score (0.0-1.0)."""
        if self == CertitudeLevel.ELEVE:
            return 1.0
        elif self == CertitudeLevel.MOYEN:
            return 0.5
        else:
            return 0.0


class ModificationNature(str, Enum):
    """Nature of a law modification."""

    CREATION = "Création"
    MODIFICATION = "Modification"
    ABROGATION = "Abrogation"


@dataclass
class LawModification:
    """A single modification to an article."""

    date: date
    law_numero: str  # "2023-1322"
    law_type: str  # "LF", "LFR", "LFSS"
    law_year: int
    article_in_law: str  # "Art. 24"
    nature: ModificationNature
    legifrance_url: str = ""
    law_title: str = ""  # Full title of the law

    @property
    def law_ref(self) -> str:
        """Return formatted law reference."""
        return f"{self.law_type} {self.law_year} (n°{self.law_numero})"

    @property
    def short_ref(self) -> str:
        """Return short reference like 'LF 2024'."""
        return f"{self.law_type} {self.law_year}"


@dataclass
class QuotedSource:
    """A quoted source from parliamentary debates."""

    citation: str
    orateur: str
    fonction: str  # rapporteur, ministre, député, sénateur
    date: str  # ISO format
    chambre: str  # "AN" or "Sénat"
    is_curated: bool = False  # True if from manually curated data

    def to_dict(self) -> dict:
        return {
            "citation": self.citation,
            "orateur": self.orateur,
            "fonction": self.fonction,
            "date": self.date,
            "chambre": self.chambre,
            "is_curated": self.is_curated,
        }


@dataclass
class SectionAnalysis:
    """Analysis for one law modification."""

    modification: LawModification
    contexte: str  # Legislative context description
    intention: str  # Summary of legislative intent
    sources_an: list[QuotedSource] = field(default_factory=list)  # AN sources
    sources_senat: list[QuotedSource] = field(default_factory=list)  # Sénat sources
    evolution_debats: str = ""  # How text evolved during debates
    points_vigilance: list[str] = field(default_factory=list)  # Rejected amendments, objections
    niveau_certitude: CertitudeLevel = CertitudeLevel.MOYEN
    certitude_justification: str = ""
    debates_analyzed: int = 0  # Number of debate chunks analyzed
    amendments_analyzed: int = 0  # Number of amendments analyzed
    operational_params: OperationalParameters | None = None  # Extracted operational parameters

    @property
    def sources(self) -> list[QuotedSource]:
        """All sources combined (for backwards compatibility)."""
        return self.sources_an + self.sources_senat

    @property
    def has_an_sources(self) -> bool:
        """Check if AN sources are available."""
        return len(self.sources_an) > 0

    @property
    def has_senat_sources(self) -> bool:
        """Check if Sénat sources are available."""
        return len(self.sources_senat) > 0


@dataclass
class ConsolidationPeriod:
    """A period between modifications where the article was consolidated."""

    start_year: int
    end_year: int
    title: str  # e.g., "Consolidation du dispositif (2021-2023)"
    doctrine_evolution: str  # How doctrine/practice evolved
    amendements_rejetes: list[str] = field(default_factory=list)  # Rejected amendments
    citations: list[QuotedSource] = field(default_factory=list)  # Key quotes from the period


@dataclass
class JurisprudenceEntry:
    """A jurisprudence entry."""

    juridiction: str  # "CE", "CC", "CADF"
    date: str
    reference: str  # "n° 472835"
    titre: str
    faits: str = ""
    solution: str = ""
    portee: str = ""


@dataclass
class ArticleInfo:
    """Information about an article from the index."""

    reference: str  # "150-0 B ter"
    titre: str
    code: str  # "CGI"
    keywords: list[str]
    modifications: list[LawModification]
    legifrance_url: str = ""
    bofip_url: str = ""


@dataclass
class OperationalParameters:
    """Operational parameters extracted from legislative intent (dynamic per article)."""

    parameters: dict[str, str] = field(default_factory=dict)


@dataclass
class SynthesisRow:
    """A row in the synthesis evolution table with dynamic columns per article."""

    periode: str
    parameters: dict[str, str] = field(default_factory=dict)


@dataclass
class ForwardLookingSection:
    """Section for ongoing PLF discussions (projets de modification)."""

    plf_year: int  # 2025, 2026
    status: str  # "en cours", "projet", "adopté"
    propositions: list[str]  # Proposed changes
    citations: list[QuotedSource] = field(default_factory=list)
    niveau_certitude: CertitudeLevel = CertitudeLevel.MOYEN


@dataclass
class CGIModificationSummary:
    """Summary of a CGI article modification within a law."""

    cgi_article_ref: str  # "150-0 B ter"
    cgi_article_titre: str
    article_in_law: str  # "Art. 24"
    nature: ModificationNature
    intention_summary: str  # Brief summary of legislative intent
    key_changes: list[str] = field(default_factory=list)
    sources_count: int = 0  # Number of source citations found
    confidence: CertitudeLevel = CertitudeLevel.MOYEN

    @property
    def confidence_score(self) -> float:
        """Return numeric confidence score (0.0-1.0)."""
        return self.confidence.score

    @classmethod
    def with_confidence_from_sources(
        cls,
        sources_count: int,
        **kwargs,
    ) -> "CGIModificationSummary":
        """Create instance with confidence derived from sources count."""
        return cls(
            sources_count=sources_count,
            confidence=CertitudeLevel.from_sources_count(sources_count),
            **kwargs,
        )


@dataclass
class LawAnalysisResult:
    """
    Full analysis document for a finance law.

    This provides a law-centric view, showing all CGI modifications
    made by a single law with their legislative intent.
    """

    # Law identification
    law_numero: str  # "2023-1322"
    law_type: str  # "LF", "LFR", "LFSS"
    law_year: int  # Budget year (e.g., 2024 for LF 2024)
    law_title: str  # Full title
    date_promulgation: str  # ISO date

    # Content
    cgi_modifications: list[CGIModificationSummary] = field(default_factory=list)
    key_themes: list[str] = field(default_factory=list)
    fiscal_context: str = ""  # Economic/fiscal context of the law

    # Statistics
    total_articles: int = 0
    total_cgi_modifications: int = 0
    articles_analyzed: int = 0

    # Metadata
    generated_date: str = ""
    dossier_an_url: str = ""
    dossier_senat_url: str = ""

    @property
    def short_ref(self) -> str:
        """Return short reference like 'LF 2024'."""
        return f"{self.law_type} {self.law_year}"

    @property
    def full_ref(self) -> str:
        """Return full reference like 'LF 2024 (n°2023-1322)'."""
        return f"{self.law_type} {self.law_year} (n°{self.law_numero})"

    @property
    def low_confidence_items(self) -> list["CGIModificationSummary"]:
        """Return modifications with low confidence that need human review."""
        return [m for m in self.cgi_modifications if m.confidence == CertitudeLevel.FAIBLE]

    @property
    def medium_confidence_items(self) -> list["CGIModificationSummary"]:
        """Return modifications with medium confidence."""
        return [m for m in self.cgi_modifications if m.confidence == CertitudeLevel.MOYEN]

    @property
    def high_confidence_items(self) -> list["CGIModificationSummary"]:
        """Return modifications with high confidence."""
        return [m for m in self.cgi_modifications if m.confidence == CertitudeLevel.ELEVE]

    @property
    def quality_score(self) -> float:
        """
        Calculate overall quality score (0.0 to 1.0).

        Score is based on:
        - Confidence levels (high=1.0, medium=0.5, low=0.0)
        - Number of sources found
        - Whether intent analysis is available
        """
        if not self.cgi_modifications:
            return 0.0

        total_score = 0.0
        for mod in self.cgi_modifications:
            # Base score from confidence
            if mod.confidence == CertitudeLevel.ELEVE:
                score = 1.0
            elif mod.confidence == CertitudeLevel.MOYEN:
                score = 0.5
            else:
                score = 0.0

            # Bonus for sources
            if mod.sources_count > 0:
                score = min(1.0, score + 0.1 * mod.sources_count)

            # Penalty if no intent summary
            if not mod.intention_summary:
                score *= 0.5

            total_score += score

        return total_score / len(self.cgi_modifications)

    @property
    def needs_review(self) -> bool:
        """Check if this analysis needs human review."""
        return len(self.low_confidence_items) > 0 or self.quality_score < 0.5


@dataclass
class BatchAnalysisState:
    """State for resumable batch processing."""

    batch_id: str
    started_at: str
    last_updated: str
    mode: str  # "laws" or "articles"
    total_items: int
    completed_items: list[str] = field(default_factory=list)
    failed_items: list[str] = field(default_factory=list)
    current_item: str | None = None

    @property
    def progress_pct(self) -> float:
        """Return progress percentage."""
        if self.total_items == 0:
            return 0.0
        return len(self.completed_items) / self.total_items * 100

    @property
    def is_complete(self) -> bool:
        """Check if batch is complete."""
        return len(self.completed_items) + len(self.failed_items) >= self.total_items


@dataclass
class ArticleAnalysisResult:
    """Full analysis document for an article."""

    # Header
    article_ref: str  # "150-0 B ter"
    article_titre: str
    article_code: str  # "CGI"
    question: str

    # Content
    historique: list[LawModification]
    sections: list[SectionAnalysis]
    consolidation_periods: list[ConsolidationPeriod] = field(default_factory=list)

    # Synthesis
    synthese_objectif: str = ""
    synthese_evolution: list[SynthesisRow] = field(default_factory=list)
    synthese_preoccupations: list[str] = field(default_factory=list)

    # Forward-looking analysis (PLF discussions)
    forward_looking: list[ForwardLookingSection] = field(default_factory=list)

    # Additional content
    jurisprudence: list[JurisprudenceEntry] = field(default_factory=list)
    doctrine_bofip: list[str] = field(default_factory=list)
    bofip_url: str = ""
    legifrance_url: str = ""

    # Enhanced methodology sources
    rapports_parlementaires: list[dict] = field(default_factory=list)
    dila_sources: list[str] = field(default_factory=list)

    # Cross-law references and PLF-LF deltas
    cross_references: list[dict] = field(default_factory=list)
    plf_lf_deltas: list[dict] = field(default_factory=list)

    # Methodology
    sources_legislatives: list[str] = field(default_factory=list)
    sources_parlementaires_an: list[str] = field(default_factory=list)
    sources_parlementaires_senat: list[str] = field(default_factory=list)
    total_debates_searched: int = 0
    total_interventions_found: int = 0

    # Metadata
    generated_date: str = ""

    @property
    def parameter_evolution(self) -> dict[str, list[tuple[str, str]]]:
        """Return parameter evolution timeline: {param_name: [(period, value), ...]}.

        Collects operational_params from all sections and groups by parameter name.
        """
        evolution: dict[str, list[tuple[str, str]]] = {}
        for section in self.sections:
            if not section.operational_params or not section.operational_params.parameters:
                continue
            periode = str(section.modification.law_year)
            for param_name, value in section.operational_params.parameters.items():
                if param_name not in evolution:
                    evolution[param_name] = []
                evolution[param_name].append((periode, value))
        return evolution

    def get_sections_with_consolidations(self) -> list:
        """
        Return sections interleaved with consolidation periods.

        This inserts consolidation periods between sections where there are
        gaps of 2+ years between modifications.
        """
        if not self.consolidation_periods:
            return list(self.sections)

        # Create a combined list with sections and consolidations
        result = []
        # Map the year BEFORE the consolidation starts to the consolidation period
        # E.g., consolidation 2020-2023 should appear after the 2019 modification
        consolidation_by_preceding_year = {
            cp.start_year - 1: cp for cp in self.consolidation_periods
        }

        # Track which consolidation periods have been inserted
        inserted = set()
        for section in self.sections:
            result.append(section)
            # Check if there's a consolidation period starting after this modification
            mod_year = section.modification.law_year
            if mod_year in consolidation_by_preceding_year:
                cp = consolidation_by_preceding_year[mod_year]
                cp_key = (cp.start_year, cp.end_year)
                if cp_key not in inserted:
                    result.append(cp)
                    inserted.add(cp_key)

        return result
