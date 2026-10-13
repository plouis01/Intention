"""LLM-as-Judge for scoring analysis quality."""

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class JudgeScore:
    """Scores from LLM judge for a single analysis."""
    case_id: str
    precision: int = 0  # 1-5: factual accuracy of citations
    pertinence: int = 0  # 1-5: relevance to the article
    completude: int = 0  # 1-5: coverage of known sources
    coherence: int = 0  # 1-5: logical coherence of intent
    comments: str = ""

    @property
    def average(self) -> float:
        return (self.precision + self.pertinence + self.completude + self.coherence) / 4.0


@dataclass
class BatchJudgeReport:
    """Aggregated report from batch judging."""
    total_cases: int = 0
    avg_precision: float = 0.0
    avg_pertinence: float = 0.0
    avg_completude: float = 0.0
    avg_coherence: float = 0.0
    avg_overall: float = 0.0
    scores: list[JudgeScore] = field(default_factory=list)


JUDGE_PROMPT = """Tu es un évaluateur expert en analyse législative française.

## Tâche
Évalue la qualité de l'analyse suivante pour l'article {article_ref} du CGI,
modification par {law_ref}.

## Analyse à évaluer
Intention: {intention}

Sources AN: {sources_an}

Sources Sénat: {sources_senat}

Certitude: {certitude}

## Références (citations vérifiées)
{reference_quotes}

## Critères d'évaluation (1 à 5 chacun)

1. **Précision** (1-5): Les citations sont-elles factuellement exactes et vérifiables ?
   - 5: Toutes les citations correspondent aux sources de référence
   - 1: Citations inventées ou déformées

2. **Pertinence** (1-5): L'analyse concerne-t-elle bien l'article spécifique ?
   - 5: Tout est directement lié à l'article
   - 1: Mélange avec d'autres articles ou sujets

3. **Complétude** (1-5): Les sources de référence sont-elles couvertes ?
   - 5: Toutes les sources de référence sont retrouvées
   - 1: Aucune source de référence n'est retrouvée

4. **Cohérence** (1-5): L'intention est-elle logique et bien structurée ?
   - 5: Argumentation claire et bien articulée
   - 1: Contradictions ou absence de logique

## Format de réponse (JSON strict)

```json
{{
  "precision": 3,
  "pertinence": 4,
  "completude": 3,
  "coherence": 4,
  "comments": "Explication brève des scores"
}}
```

Réponds UNIQUEMENT avec le JSON.
"""


class LLMJudge:
    """Score analysis quality using LLM-as-Judge."""

    def __init__(self, call_llm_fn=None):
        """
        Args:
            call_llm_fn: Function that takes (prompt, temperature) and returns response text.
                         If None, will try to use the ArticleAnalyzer's _call_llm.
        """
        self._call_llm = call_llm_fn

    def _ensure_llm(self):
        if self._call_llm is None:
            try:
                from src.analysis.article_analyzer import ArticleAnalyzer
                analyzer = ArticleAnalyzer()
                self._call_llm = analyzer._call_llm
            except Exception as e:
                raise RuntimeError(f"No LLM available for judging: {e}")

    def score_analysis(
        self,
        case_id: str,
        article_ref: str,
        law_ref: str,
        intention: str,
        sources_an: list[dict],
        sources_senat: list[dict],
        certitude: str,
        reference_quotes: list[dict],
    ) -> JudgeScore:
        """Score a single analysis against reference data."""
        self._ensure_llm()

        # Format reference quotes
        ref_text = ""
        for q in reference_quotes:
            ref_text += f"- [{q.get('orateur', '?')}] {q.get('citation_substring', '')}\n"
        if not ref_text:
            ref_text = "[Aucune référence disponible]"

        # Format sources
        an_text = "\n".join(
            f"- [{s.get('orateur', '?')}] {s.get('citation', '')[:200]}"
            for s in sources_an
        ) or "[Aucune source AN]"

        senat_text = "\n".join(
            f"- [{s.get('orateur', '?')}] {s.get('citation', '')[:200]}"
            for s in sources_senat
        ) or "[Aucune source Sénat]"

        prompt = JUDGE_PROMPT.format(
            article_ref=article_ref,
            law_ref=law_ref,
            intention=intention[:500],
            sources_an=an_text,
            sources_senat=senat_text,
            certitude=certitude,
            reference_quotes=ref_text,
        )

        try:
            response_text = self._call_llm(prompt, temperature=0.0)
            # Parse JSON
            text = response_text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            result = json.loads(text.strip())

            return JudgeScore(
                case_id=case_id,
                precision=int(result.get("precision", 0)),
                pertinence=int(result.get("pertinence", 0)),
                completude=int(result.get("completude", 0)),
                coherence=int(result.get("coherence", 0)),
                comments=result.get("comments", ""),
            )
        except Exception as e:
            logger.error(f"LLM judge failed for {case_id}: {e}")
            return JudgeScore(case_id=case_id, comments=f"Error: {e}")

    def batch_score(self, analyses: list[dict]) -> BatchJudgeReport:
        """Score multiple analyses and aggregate.

        Each item in analyses should have:
            case_id, article_ref, law_ref, intention, sources_an, sources_senat,
            certitude, reference_quotes
        """
        scores = []
        for item in analyses:
            score = self.score_analysis(**item)
            scores.append(score)

        if not scores:
            return BatchJudgeReport()

        n = len(scores)
        return BatchJudgeReport(
            total_cases=n,
            avg_precision=round(sum(s.precision for s in scores) / n, 2),
            avg_pertinence=round(sum(s.pertinence for s in scores) / n, 2),
            avg_completude=round(sum(s.completude for s in scores) / n, 2),
            avg_coherence=round(sum(s.coherence for s in scores) / n, 2),
            avg_overall=round(sum(s.average for s in scores) / n, 2),
            scores=scores,
        )
