"""Regression tests parametrized on golden evaluation dataset.

Skips if no embeddings are available (requires data/embeddings/).
"""

import json
from pathlib import Path

import pytest

GOLDEN_SET_PATH = Path("data/eval/golden_set.json")
EMBEDDINGS_DIR = Path("data/embeddings")

# Skip all tests if prerequisites are missing
pytestmark = [
    pytest.mark.skipif(
        not GOLDEN_SET_PATH.exists(),
        reason="Golden set not found (run scripts/build_golden_set.py first)",
    ),
    pytest.mark.skipif(
        not EMBEDDINGS_DIR.exists(),
        reason="Embeddings directory not found (run --embed pipeline phase first)",
    ),
]


def _load_golden_cases():
    """Load golden cases for parametrize."""
    if not GOLDEN_SET_PATH.exists():
        return []
    with open(GOLDEN_SET_PATH) as f:
        data = json.load(f)
    return data.get("cases", [])


def _get_case_ids():
    """Get case IDs for parametrize."""
    cases = _load_golden_cases()
    return [c["id"] for c in cases]


@pytest.fixture(scope="module")
def evaluator():
    """Create evaluator with search service loaded."""
    from src.eval.evaluator import PipelineEvaluator
    return PipelineEvaluator()


@pytest.fixture(scope="module")
def golden_cases():
    """Load golden cases."""
    from src.eval.evaluator import load_golden_set
    return load_golden_set(GOLDEN_SET_PATH)


class TestSearchQuality:
    """Test search quality metrics against golden set."""

    def test_overall_recall(self, evaluator, golden_cases):
        """Overall recall should be above minimum threshold."""
        report = evaluator.evaluate_search_quality(golden_cases)
        assert report.recall_at_k >= 0.3, (
            f"Overall recall {report.recall_at_k:.3f} below minimum 0.3"
        )

    def test_overall_precision(self, evaluator, golden_cases):
        """Overall precision should be above minimum threshold."""
        report = evaluator.evaluate_search_quality(golden_cases)
        assert report.precision_at_k >= 0.5, (
            f"Overall precision {report.precision_at_k:.3f} below minimum 0.5"
        )

    def test_mrr_above_minimum(self, evaluator, golden_cases):
        """MRR should indicate relevant results in top positions."""
        report = evaluator.evaluate_search_quality(golden_cases)
        assert report.mrr >= 0.2, (
            f"MRR {report.mrr:.3f} below minimum 0.2"
        )


class TestChunkFiltering:
    """Test chunk filtering quality."""

    def test_must_find_survival(self, evaluator, golden_cases):
        """Must-find chunks should survive filtering."""
        report = evaluator.evaluate_chunk_filtering(golden_cases)
        assert report.must_find_survival_rate >= 0.3, (
            f"Must-find survival {report.must_find_survival_rate:.3f} below minimum 0.3"
        )

    def test_must_not_find_rejection(self, evaluator, golden_cases):
        """Must-not-find contamination should be rejected."""
        report = evaluator.evaluate_chunk_filtering(golden_cases)
        assert report.must_not_find_rejection_rate >= 0.5, (
            f"Must-not-find rejection {report.must_not_find_rejection_rate:.3f} below minimum 0.5"
        )


class TestRegressionBaseline:
    """Test against saved baseline if available."""

    BASELINE_PATH = Path("data/eval/baseline.json")

    @pytest.mark.skipif(
        not Path("data/eval/baseline.json").exists(),
        reason="No baseline saved yet",
    )
    def test_no_recall_regression(self, evaluator, golden_cases):
        """Recall should not regress vs baseline."""
        with open(self.BASELINE_PATH) as f:
            baseline = json.load(f)

        report = evaluator.evaluate_search_quality(golden_cases)
        baseline_recall = baseline.get("search", {}).get("recall", 0)

        assert report.recall_at_k >= baseline_recall - 0.05, (
            f"Recall regressed: {baseline_recall:.3f} → {report.recall_at_k:.3f}"
        )

    @pytest.mark.skipif(
        not Path("data/eval/baseline.json").exists(),
        reason="No baseline saved yet",
    )
    def test_no_precision_regression(self, evaluator, golden_cases):
        """Precision should not regress vs baseline."""
        with open(self.BASELINE_PATH) as f:
            baseline = json.load(f)

        report = evaluator.evaluate_search_quality(golden_cases)
        baseline_precision = baseline.get("search", {}).get("precision", 0)

        assert report.precision_at_k >= baseline_precision - 0.05, (
            f"Precision regressed: {baseline_precision:.3f} → {report.precision_at_k:.3f}"
        )
