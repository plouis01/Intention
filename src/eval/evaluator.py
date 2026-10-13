"""Pipeline evaluator for search quality and filtering."""
import json
import logging
from pathlib import Path

from src.eval.models import (
    FilteringReport,
    GoldenCase,
    SearchQualityReport,
    ThresholdSweepResult,
)

logger = logging.getLogger(__name__)

DEFAULT_GOLDEN_SET_PATH = Path(__file__).parent.parent.parent / "data" / "eval" / "golden_set.json"


def load_golden_set(path: Path | None = None) -> list[GoldenCase]:
    """Load golden evaluation dataset."""
    path = path or DEFAULT_GOLDEN_SET_PATH
    if not path.exists():
        raise FileNotFoundError(f"Golden set not found: {path}")
    with open(path) as f:
        data = json.load(f)
    cases = []
    for item in data.get("cases", []):
        cases.append(GoldenCase(
            id=item["id"],
            article_ref=item["article_ref"],
            law_type=item["law_type"],
            law_year=item["law_year"],
            must_find=item.get("must_find", []),
            must_not_find=item.get("must_not_find", []),
            expected_quotes=item.get("expected_quotes", []),
            expected_intention_keywords=item.get("expected_intention_keywords", []),
            expected_certitude=item.get("expected_certitude", "moyen"),
        ))
    return cases


class PipelineEvaluator:
    """Evaluates the analysis pipeline against golden test cases."""

    def __init__(self, search_service=None):
        self.search_service = search_service

    def _ensure_search_service(self):
        if self.search_service is None:
            from src.indexing.search import SearchService
            from src.utils.config import get_settings

            settings = get_settings()
            self.search_service = SearchService()

            if settings.vector_store_provider == "pgvector":
                from src.indexing.vector_stores import create_vector_store
                logger.info("Eval: connecting to pgvector store...")
                pgvector_store = create_vector_store("pgvector")
                self.search_service.debate_store = pgvector_store
                self.search_service.amendment_store = pgvector_store
                logger.info(f"Eval: pgvector connected ({pgvector_store.count()} chunks)")
            else:
                embeddings_dir = Path("data/embeddings")
                if embeddings_dir.exists():
                    self.search_service.load_stores(embeddings_dir)

    def evaluate_search_quality(
        self, cases: list[GoldenCase], top_k: int = 15
    ) -> SearchQualityReport:
        """Evaluate search quality against golden cases (offline, no LLM)."""
        self._ensure_search_service()

        total_recall = 0.0
        total_precision = 0.0
        total_rr = 0.0  # reciprocal rank sum
        per_case = []

        for case in cases:
            from datetime import date
            # Build search params
            law_year = case.law_year
            if case.law_type == "LF":
                date_start = date(law_year - 1, 3, 1)
                date_end = date(law_year, 2, 28)
            else:
                date_start = date(law_year - 1, 6, 1)
                date_end = date(law_year, 3, 31)

            # Get article keywords and modification info from index
            from src.analysis.article_index import get_article_index
            index = get_article_index()
            article_info = index.get_article(case.article_ref)
            if not article_info:
                logger.warning(f"Article {case.article_ref} not found in index, skipping")
                continue
            keywords = article_info.keywords

            # Find matching modification to get article_in_law
            article_in_law = ""
            for mod in article_info.modifications:
                if mod.law_type == case.law_type and mod.law_year == case.law_year:
                    article_in_law = mod.article_in_law
                    break

            try:
                results = self.search_service.search_article_modification(
                    article_ref=case.article_ref,
                    article_keywords=keywords,
                    law_type=case.law_type,
                    law_year=case.law_year,
                    date_start=date_start,
                    date_end=date_end,
                    top_k=top_k,
                    min_relevant=3,
                    article_in_law=article_in_law,
                )
            except Exception as e:
                logger.error(f"Search failed for {case.id}: {e}")
                per_case.append({"case_id": case.id, "error": str(e)})
                continue

            debate_chunks = results.get("debates", [])
            chunk_texts = [chunk.content.lower() for chunk, _ in debate_chunks]

            # Recall: fraction of must_find in results
            found_count = 0
            first_found_rank = None
            for i, mf in enumerate(case.must_find):
                substring = mf.get("content_substring", "").lower()
                if not substring:
                    continue
                for rank, text in enumerate(chunk_texts):
                    if substring in text:
                        found_count += 1
                        if first_found_rank is None:
                            first_found_rank = rank + 1
                        break

            recall = found_count / max(len(case.must_find), 1)
            rr = 1.0 / first_found_rank if first_found_rank else 0.0

            # Precision: fraction NOT in must_not_find
            contaminated = 0
            for mnf in case.must_not_find:
                substring = mnf.get("content_substring", "").lower()
                if not substring:
                    continue
                for text in chunk_texts:
                    if substring in text:
                        contaminated += 1
                        break

            precision = 1.0 - (contaminated / max(len(chunk_texts), 1))

            total_recall += recall
            total_precision += precision
            total_rr += rr

            per_case.append({
                "case_id": case.id,
                "recall": round(recall, 3),
                "precision": round(precision, 3),
                "mrr": round(rr, 3),
                "chunks_found": len(debate_chunks),
                "must_find_found": found_count,
                "must_find_total": len(case.must_find),
                "contaminated": contaminated,
            })

        n = max(len(per_case), 1)
        return SearchQualityReport(
            total_cases=len(per_case),
            recall_at_k=round(total_recall / n, 3),
            precision_at_k=round(total_precision / n, 3),
            mrr=round(total_rr / n, 3),
            per_case=per_case,
        )

    def evaluate_chunk_filtering(
        self, cases: list[GoldenCase], top_k: int = 15
    ) -> FilteringReport:
        """Evaluate how well filtering preserves must_find and rejects must_not_find."""
        self._ensure_search_service()

        total_survival = 0.0
        total_rejection = 0.0
        per_case = []

        for case in cases:
            from datetime import date
            law_year = case.law_year
            if case.law_type == "LF":
                date_start = date(law_year - 1, 3, 1)
                date_end = date(law_year, 2, 28)
            else:
                date_start = date(law_year - 1, 6, 1)
                date_end = date(law_year, 3, 31)

            from src.analysis.article_index import get_article_index
            index = get_article_index()
            article_info = index.get_article(case.article_ref)
            if not article_info:
                continue

            # Find matching modification to get article_in_law
            article_in_law = ""
            for mod in article_info.modifications:
                if mod.law_type == case.law_type and mod.law_year == case.law_year:
                    article_in_law = mod.article_in_law
                    break

            try:
                results = self.search_service.search_article_modification(
                    article_ref=case.article_ref,
                    article_keywords=article_info.keywords,
                    law_type=case.law_type,
                    law_year=case.law_year,
                    date_start=date_start,
                    date_end=date_end,
                    top_k=top_k * 2,
                    min_relevant=1,
                    article_in_law=article_in_law,
                )
            except Exception as e:
                logger.error(f"Search failed for {case.id}: {e}")
                continue

            debate_chunks = results.get("debates", [])
            chunk_texts = [chunk.content.lower() for chunk, _ in debate_chunks]

            # Survival of must_find
            survived = 0
            for mf in case.must_find:
                substring = mf.get("content_substring", "").lower()
                if any(substring in t for t in chunk_texts):
                    survived += 1

            survival = survived / max(len(case.must_find), 1)

            # Rejection of must_not_find
            rejected = 0
            for mnf in case.must_not_find:
                substring = mnf.get("content_substring", "").lower()
                if not any(substring in t for t in chunk_texts):
                    rejected += 1

            rejection = rejected / max(len(case.must_not_find), 1)

            total_survival += survival
            total_rejection += rejection
            per_case.append({
                "case_id": case.id,
                "survival_rate": round(survival, 3),
                "rejection_rate": round(rejection, 3),
            })

        n = max(len(per_case), 1)
        return FilteringReport(
            total_cases=len(per_case),
            must_find_survival_rate=round(total_survival / n, 3),
            must_not_find_rejection_rate=round(total_rejection / n, 3),
            per_case=per_case,
        )

    def compute_threshold_sensitivity(
        self,
        param_name: str,
        values: list[float],
        cases: list[GoldenCase],
        top_k: int = 15,
    ) -> ThresholdSweepResult:
        """Sweep a single threshold and report metrics at each value."""
        import os
        metrics = []
        best_recall = -1.0
        best_value = values[0]

        for value in values:
            # Set threshold via env var so get_settings() picks it up
            env_key = param_name.upper()
            os.environ[env_key] = str(value)

            # Clear settings cache to pick up new value
            from src.utils.config import get_settings
            get_settings.cache_clear()

            report = self.evaluate_search_quality(cases, top_k=top_k)
            metrics.append({
                "value": value,
                "recall": report.recall_at_k,
                "precision": report.precision_at_k,
                "mrr": report.mrr,
            })

            if report.recall_at_k > best_recall:
                best_recall = report.recall_at_k
                best_value = value

        # Restore env
        if param_name.upper() in os.environ:
            del os.environ[param_name.upper()]
        from src.utils.config import get_settings
        get_settings.cache_clear()

        return ThresholdSweepResult(
            parameter_name=param_name,
            values=values,
            metrics=metrics,
            best_value=best_value,
            best_metric="recall_at_k",
        )
