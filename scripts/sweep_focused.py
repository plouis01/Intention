#!/usr/bin/env python3
"""Focused threshold sweep on working cases only (non-zero recall).

Sweeps search_min_similarity, search_keyword_boost, and reranker_min_score
one at a time, holding the others at current defaults. Reports per-case
details and optimal values.

Run with: python3 -u scripts/sweep_focused.py
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

# Force ONNX Runtime to use CPU only (avoid CoreML crash on Apple Silicon)
try:
    import onnxruntime as _ort
    _original_providers = _ort.get_available_providers
    def _cpu_only_providers():
        return [p for p in _original_providers() if p != "CoreMLExecutionProvider"]
    _ort.get_available_providers = _cpu_only_providers
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent.parent))

# Configure logging to see progress
logging.basicConfig(
    level=logging.WARNING,  # Only show warnings/errors to keep output clean
    format="%(levelname)s: %(message)s",
    stream=sys.stderr,
)

from src.eval.evaluator import PipelineEvaluator, load_golden_set

# Cases known to have non-zero recall (data coverage exists)
WORKING_CASE_IDS = {
    "150_0_b_ter_lf_2025",
    "787_b_lf_2016",
    "787_b_lf_2019",
    "787_b_lf_2023",
    "787_b_lf_2024",
    "199_terdecies_0_a_lf_2013",
    "199_terdecies_0_a_lf_2022",
    "199_terdecies_0_a_lf_2023",
    "199_terdecies_0_a_lf_2025",
    "200_lf_2025",
    "44_sexies_a_lf_2023",
    "44_sexies_lfr_2003",
    "217_undecies_lfr_2014",
    "238_bis_lf_2017",
}

# Parameters to sweep and their candidate values
SWEEPS = {
    "search_min_similarity": [0.10, 0.15, 0.20, 0.25, 0.30],
    "search_keyword_boost": [0.3, 0.4, 0.5, 0.6, 0.7],
    "reranker_min_score": [0.01, 0.05, 0.1, 0.2, 0.3],
}


def log(msg):
    """Print with flush for unbuffered output."""
    print(msg, flush=True)


def run_eval(evaluator, cases, label=""):
    """Run eval and return report, printing progress per case."""
    t0 = time.time()
    report = evaluator.evaluate_search_quality(cases)
    elapsed = time.time() - t0
    if label:
        log(f"  [{label}] recall={report.recall_at_k:.3f}  "
            f"precision={report.precision_at_k:.3f}  "
            f"mrr={report.mrr:.3f}  ({elapsed:.1f}s)")
    return report


def main():
    golden_path = Path("data/eval/golden_set.json")
    if not golden_path.exists():
        log("Golden set not found at data/eval/golden_set.json")
        sys.exit(1)

    all_cases = load_golden_set(golden_path)
    working_cases = [c for c in all_cases if c.id in WORKING_CASE_IDS]
    log(f"Loaded {len(all_cases)} total cases, using {len(working_cases)} working cases")
    log(f"Working case IDs: {sorted(c.id for c in working_cases)}")

    evaluator = PipelineEvaluator()

    # Force search service initialization (one-time cost)
    log("\nInitializing search service and reranker (one-time)...")
    evaluator._ensure_search_service()
    log("Search service ready.")

    # Baseline on working subset
    log("\n" + "=" * 60)
    log("BASELINE (working cases only)")
    log("=" * 60)
    baseline = run_eval(evaluator, working_cases, label="baseline")
    log(f"  Recall@K:    {baseline.recall_at_k:.3f}")
    log(f"  Precision@K: {baseline.precision_at_k:.3f}")
    log(f"  MRR:         {baseline.mrr:.3f}")
    log(f"  Cases evaluated: {baseline.total_cases}")

    # Print per-case baseline
    log("\n  Per-case baseline:")
    for pc in baseline.per_case:
        recall_str = f"recall={pc.get('recall', 0):.3f}"
        prec_str = f"precision={pc.get('precision', 0):.3f}"
        mrr_str = f"mrr={pc.get('mrr', 0):.3f}"
        found_str = f"found={pc.get('must_find_found', 0)}/{pc.get('must_find_total', 0)}"
        chunks_str = f"chunks={pc.get('chunks_found', 0)}"
        log(f"    {pc['case_id']:45s} {recall_str}  {prec_str}  {mrr_str}  {found_str}  {chunks_str}")

    # Sweep each parameter
    all_results = {
        "baseline": {
            "recall": baseline.recall_at_k,
            "precision": baseline.precision_at_k,
            "mrr": baseline.mrr,
            "total_cases": baseline.total_cases,
            "per_case": baseline.per_case,
        },
        "sweeps": {},
    }

    for param_name, values in SWEEPS.items():
        log(f"\n{'=' * 60}")
        log(f"SWEEPING {param_name}: {values}")
        log("=" * 60)
        sweep_start = time.time()

        sweep_metrics = []
        best_recall = -1.0
        best_value = values[0]
        best_per_case = []

        for value in values:
            # Set threshold via env var
            env_key = param_name.upper()
            os.environ[env_key] = str(value)

            # Clear settings cache
            from src.utils.config import get_settings
            get_settings.cache_clear()

            report = run_eval(evaluator, working_cases, label=f"{param_name}={value}")

            m = {
                "value": value,
                "recall": report.recall_at_k,
                "precision": report.precision_at_k,
                "mrr": report.mrr,
                "per_case": report.per_case,
            }
            sweep_metrics.append(m)

            if report.recall_at_k > best_recall or (
                report.recall_at_k == best_recall and report.mrr > (
                    next((sm["mrr"] for sm in sweep_metrics if sm["value"] == best_value), 0)
                )
            ):
                best_recall = report.recall_at_k
                best_value = value
                best_per_case = report.per_case

        elapsed = time.time() - sweep_start
        log(f"\n  Best {param_name} = {best_value} (recall={best_recall:.3f}) [{elapsed:.1f}s total]")

        # Show per-case for best value
        log(f"\n  Per-case at best value ({param_name}={best_value}):")
        for pc in best_per_case:
            recall_str = f"recall={pc.get('recall', 0):.3f}"
            prec_str = f"precision={pc.get('precision', 0):.3f}"
            mrr_str = f"mrr={pc.get('mrr', 0):.3f}"
            found_str = f"found={pc.get('must_find_found', 0)}/{pc.get('must_find_total', 0)}"
            chunks_str = f"chunks={pc.get('chunks_found', 0)}"
            log(f"    {pc['case_id']:45s} {recall_str}  {prec_str}  {mrr_str}  {found_str}  {chunks_str}")

        # Restore env
        if param_name.upper() in os.environ:
            del os.environ[param_name.upper()]
        from src.utils.config import get_settings
        get_settings.cache_clear()

        all_results["sweeps"][param_name] = {
            "values": values,
            "metrics": [{k: v for k, v in m.items() if k != "per_case"} for m in sweep_metrics],
            "best_value": best_value,
            "best_recall": best_recall,
        }

    # Summary
    log(f"\n{'=' * 60}")
    log("SUMMARY")
    log("=" * 60)
    log(f"Baseline (working only):  recall={all_results['baseline']['recall']:.3f}  "
        f"precision={all_results['baseline']['precision']:.3f}  "
        f"mrr={all_results['baseline']['mrr']:.3f}")
    log("")
    for param_name, sweep_data in all_results["sweeps"].items():
        best_val = sweep_data["best_value"]
        best_rec = sweep_data["best_recall"]
        best_m = next(m for m in sweep_data["metrics"] if m["value"] == best_val)
        log(f"  {param_name:30s} best={best_val}  "
            f"recall={best_rec:.3f}  "
            f"precision={best_m['precision']:.3f}  "
            f"mrr={best_m['mrr']:.3f}")

    # Write results
    output_path = Path("data/eval/sweep_focused_results.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    log(f"\nResults written to {output_path}")


if __name__ == "__main__":
    main()
