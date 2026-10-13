"""Data models for the evaluation framework."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GoldenCase:
    """A golden evaluation test case."""
    id: str  # e.g. "150_0_b_ter_lf_2019"
    article_ref: str  # "150-0 B ter"
    law_type: str  # "LF"
    law_year: int
    must_find: list[dict] = field(default_factory=list)
    must_not_find: list[dict] = field(default_factory=list)
    expected_quotes: list[dict] = field(default_factory=list)
    expected_intention_keywords: list[str] = field(default_factory=list)
    expected_certitude: str = "moyen"  # "eleve", "moyen", "faible"


@dataclass
class SearchQualityReport:
    """Report from search quality evaluation."""
    total_cases: int = 0
    recall_at_k: float = 0.0  # fraction of must_find in top-K
    precision_at_k: float = 0.0  # fraction of top-K NOT in must_not_find
    mrr: float = 0.0  # Mean Reciprocal Rank for first must_find hit
    per_case: list[dict] = field(default_factory=list)  # per-case breakdown


@dataclass
class FilteringReport:
    """Report from chunk filtering evaluation."""
    total_cases: int = 0
    must_find_survival_rate: float = 0.0  # fraction of must_find surviving filtering
    must_not_find_rejection_rate: float = 0.0  # fraction of must_not_find rejected
    per_case: list[dict] = field(default_factory=list)


@dataclass
class QuoteReport:
    """Report from quote validation evaluation (requires LLM)."""
    total_cases: int = 0
    quote_precision: float = 0.0  # fraction of extracted quotes matching expected
    quote_recall: float = 0.0  # fraction of expected quotes found in extraction
    certitude_accuracy: float = 0.0  # fraction of correct certitude levels
    per_case: list[dict] = field(default_factory=list)


@dataclass
class ThresholdSweepResult:
    """Result from sweeping a single threshold."""
    parameter_name: str
    values: list[float] = field(default_factory=list)
    metrics: list[dict] = field(default_factory=list)  # one SearchQualityReport dict per value
    best_value: float = 0.0
    best_metric: str = ""  # which metric was optimized
