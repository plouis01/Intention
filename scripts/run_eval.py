#!/usr/bin/env python3
"""Master evaluation script for the analysis pipeline.

Usage:
    python scripts/run_eval.py --search-only       # Offline search quality eval
    python scripts/run_eval.py --full               # Includes LLM judge (when API available)
    python scripts/run_eval.py --update-baseline    # Save current metrics as baseline
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

BASELINE_PATH = Path("data/eval/baseline.json")
GOLDEN_SET_PATH = Path("data/eval/golden_set.json")


def run_search_eval():
    """Run offline search quality evaluation."""
    from src.eval.evaluator import PipelineEvaluator, load_golden_set

    if not GOLDEN_SET_PATH.exists():
        print("Golden set not found. Run: python scripts/build_golden_set.py")
        return None

    cases = load_golden_set(GOLDEN_SET_PATH)
    print(f"Loaded {len(cases)} golden cases")

    evaluator = PipelineEvaluator()

    # Search quality
    print("\n=== Search Quality ===")
    search_report = evaluator.evaluate_search_quality(cases)
    print(f"  Recall@K:    {search_report.recall_at_k:.3f}")
    print(f"  Precision@K: {search_report.precision_at_k:.3f}")
    print(f"  MRR:         {search_report.mrr:.3f}")

    # Filtering quality
    print("\n=== Chunk Filtering ===")
    filter_report = evaluator.evaluate_chunk_filtering(cases)
    print(f"  Must-find survival:    {filter_report.must_find_survival_rate:.3f}")
    print(f"  Must-not-find reject:  {filter_report.must_not_find_rejection_rate:.3f}")

    # Per-case details
    print("\n=== Per-Case Breakdown ===")
    for case_result in search_report.per_case:
        case_id = case_result.get("case_id", "?")
        recall = case_result.get("recall", 0)
        precision = case_result.get("precision", 0)
        status = "OK" if recall >= 0.5 else "LOW"
        print(f"  [{status}] {case_id}: recall={recall:.2f} precision={precision:.2f}")

    return {
        "search": {
            "recall": search_report.recall_at_k,
            "precision": search_report.precision_at_k,
            "mrr": search_report.mrr,
            "total_cases": search_report.total_cases,
        },
        "filtering": {
            "survival_rate": filter_report.must_find_survival_rate,
            "rejection_rate": filter_report.must_not_find_rejection_rate,
        },
    }


def run_llm_judge():
    """Run LLM-as-Judge evaluation (requires API access)."""
    from src.eval.evaluator import load_golden_set
    from src.eval.llm_judge import LLMJudge

    if not GOLDEN_SET_PATH.exists():
        print("Golden set not found.")
        return None

    cases = load_golden_set(GOLDEN_SET_PATH)
    print(f"\n=== LLM Judge ({len(cases)} cases) ===")

    try:
        LLMJudge()  # Check if LLM is available
        # Judge requires full analysis results which we don't have in this script
        # This is a placeholder for when full analysis results are available
        print("  LLM judge requires analysis results. Run analysis first, then judge.")
        return None
    except Exception as e:
        print(f"  LLM judge unavailable: {e}")
        return None


def check_regression(current_metrics: dict):
    """Check for regressions against baseline."""
    if not BASELINE_PATH.exists():
        print("\n=== No baseline found. Run with --update-baseline to create one. ===")
        return

    with open(BASELINE_PATH) as f:
        baseline = json.load(f)

    print("\n=== Regression Check ===")
    regressions = []

    for category, metrics in current_metrics.items():
        if category not in baseline:
            continue
        for metric_name, value in metrics.items():
            baseline_value = baseline[category].get(metric_name)
            if baseline_value is None:
                continue
            if isinstance(value, (int, float)) and isinstance(baseline_value, (int, float)):
                delta = value - baseline_value
                status = "OK" if delta >= -0.02 else "REGRESSION"
                print(
                    f"  [{status}] {category}.{metric_name}: "
                    f"{baseline_value:.3f} → {value:.3f} ({delta:+.3f})"
                )
                if delta < -0.02:
                    regressions.append(f"{category}.{metric_name}")

    if regressions:
        print(f"\n  WARNING: {len(regressions)} regression(s) detected!")
    else:
        print("\n  All metrics within tolerance.")


def update_baseline(metrics: dict):
    """Save current metrics as baseline."""
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_PATH, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"\nBaseline saved to {BASELINE_PATH}")


def main():
    parser = argparse.ArgumentParser(description="Evaluation pipeline")
    parser.add_argument("--search-only", action="store_true", help="Offline search quality eval")
    parser.add_argument("--full", action="store_true", help="Include LLM judge")
    parser.add_argument("--update-baseline", action="store_true", help="Save metrics as baseline")
    args = parser.parse_args()

    if not args.search_only and not args.full and not args.update_baseline:
        args.search_only = True  # Default to search-only

    metrics = run_search_eval()
    if metrics is None:
        sys.exit(1)

    if args.full:
        llm_metrics = run_llm_judge()
        if llm_metrics:
            metrics["llm_judge"] = llm_metrics

    check_regression(metrics)

    if args.update_baseline:
        update_baseline(metrics)


if __name__ == "__main__":
    main()
