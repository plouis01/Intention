"""Data models for article analysis."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date
from enum import Enum


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
    CODIFICATION = "Codification"
    TRANSFERT = "Transfert"


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
    objet: str = ""  # Description of the modification from Légifrance
    date_debut_vigueur: str = ""  # Start of validity period
    date_fin_vigueur: str = ""  # End of validity period
    nor: str = ""  # NOR number of the modifying text

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
    passage_cle: str = ""  # Original LLM passage_cle for traceability
    extrait_id: str = ""  # Original LLM extrait_id (EXTRAIT-N or AMEND-N)

    def to_dict(self) -> dict:
        d = {
            "citation": self.citation,
            "orateur": self.orateur,
            "fonction": self.fonction,
            "date": self.date,
            "chambre": self.chambre,
            "is_curated": self.is_curated,
        }
        if self.passage_cle:
            d["passage_cle"] = self.passage_cle
        if self.extrait_id:
            d["extrait_id"] = self.extrait_id
        return d


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
    amendements_rejetes_detailles: list[dict] = field(default_factory=list)  # Structured rejected amendments
    niveau_certitude: CertitudeLevel = CertitudeLevel.MOYEN
    certitude_justification: str = ""
    debates_analyzed: int = 0  # Number of debate chunks analyzed
    amendments_analyzed: int = 0  # Number of amendments analyzed
    operational_params: OperationalParameters | None = None  # Extracted operational parameters
    doctrine_convergence: str = ""  # How doctrine/jurisprudence aligns: confirme|contredit|silencieux
    doctrine_references: list[str] = field(default_factory=list)  # BOFiP/CE refs cited

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
class ArticleInfo:
    """Information about an article from the index."""

    reference: str  # "150-0 B ter"
    titre: str
    code: str  # "CGI"
    keywords: list[str]
    modifications: list[LawModification]
    legifrance_url: str = ""
    etat_actuel: str = ""  # "VIGUEUR", "ABROGE", etc.
    texte_actuel: str = ""  # Full current text of the article
    nota: str = ""  # Application conditions / nota
    hierarchie: list[str] = field(default_factory=list)  # Position in CGI hierarchy
    article_ids: list[str] = field(default_factory=list)  # LEGIARTI historical IDs
    references_croisees: list[str] = field(default_factory=list)  # Cross-referenced articles


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
    synthese_evolution: list[SynthesisRow] = field(default_factory=list)

    # Additional content
    legifrance_url: str = ""
    etat_actuel: str = ""  # "VIGUEUR", "ABROGE", etc.

    # Cross-law references
    cross_references: list[dict] = field(default_factory=list)

    # Jurisprudence (verified from DB, not LLM-generated)
    jurisprudence: list[dict] = field(default_factory=list)

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


# ============================================================================
# Token Usage Tracking
# ============================================================================


@dataclass
class TokenUsage:
    """Token usage from a single LLM call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    phase: str = ""
    provider: str = ""


@dataclass
class TokenTracker:
    """Accumulates token usage across all LLM calls of one analysis."""

    calls: list[TokenUsage] = field(default_factory=list)
    _current_phase: str = field(default="", repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # Haiku 4.5 pricing ($ per million tokens)
    PRICING: dict = field(default_factory=lambda: {
        "anthropic": {"input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10},
        "groq": {"input": 0.0, "output": 0.0, "cache_write": 0.0, "cache_read": 0.0},
    }, repr=False)

    def set_phase(self, phase: str) -> None:
        self._current_phase = phase

    def record(self, usage: TokenUsage) -> None:
        with self._lock:
            self.calls.append(usage)

    @property
    def total_input(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def total_output(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    @property
    def total_cache_write(self) -> int:
        return sum(c.cache_creation_input_tokens for c in self.calls)

    @property
    def total_cache_read(self) -> int:
        return sum(c.cache_read_input_tokens for c in self.calls)

    def estimated_cost_usd(self) -> float:
        cost = 0.0
        for c in self.calls:
            p = self.PRICING.get(c.provider, self.PRICING["groq"])
            cost += c.input_tokens * p["input"] / 1_000_000
            cost += c.output_tokens * p["output"] / 1_000_000
            cost += c.cache_creation_input_tokens * p["cache_write"] / 1_000_000
            cost += c.cache_read_input_tokens * p["cache_read"] / 1_000_000
        return round(cost, 6)

    @staticmethod
    def summarize_calls(calls: list[TokenUsage]) -> dict:
        """Summarize a subset of calls (e.g. for a single section)."""
        return {
            "calls": len(calls),
            "input_tokens": sum(c.input_tokens for c in calls),
            "output_tokens": sum(c.output_tokens for c in calls),
            "cache_write_tokens": sum(c.cache_creation_input_tokens for c in calls),
            "cache_read_tokens": sum(c.cache_read_input_tokens for c in calls),
        }

    def to_dict(self) -> dict:
        by_phase: dict[str, dict] = {}
        for c in self.calls:
            phase = c.phase or "unknown"
            if phase not in by_phase:
                by_phase[phase] = {
                    "calls": 0, "input_tokens": 0, "output_tokens": 0,
                    "cache_write_tokens": 0, "cache_read_tokens": 0,
                }
            bp = by_phase[phase]
            bp["calls"] += 1
            bp["input_tokens"] += c.input_tokens
            bp["output_tokens"] += c.output_tokens
            bp["cache_write_tokens"] += c.cache_creation_input_tokens
            bp["cache_read_tokens"] += c.cache_read_input_tokens

        return {
            "total_calls": len(self.calls),
            "total_input_tokens": self.total_input,
            "total_output_tokens": self.total_output,
            "total_cache_write_tokens": self.total_cache_write,
            "total_cache_read_tokens": self.total_cache_read,
            "estimated_cost_usd": self.estimated_cost_usd(),
            "by_phase": by_phase,
        }

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
