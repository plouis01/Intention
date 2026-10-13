#!/usr/bin/env python3
"""Sweep search thresholds against golden evaluation dataset.

Runs offline (search only, no LLM). Writes results to data/eval/sweep_results.json.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.eval.evaluator import PipelineEvaluator, load_golden_set

# Thresholds to sweep with their config parameter names and value ranges
THRESHOLD_SWEEPS = {
    "search_min_similarity": [0.15, 0.20, 0.25, 0.30, 0.35],
    "search_keyword_boost": [0.3, 0.4, 0.5, 0.6, 0.7],
    "search_tier2_min_required": [2, 3, 4],
    "reranker_min_score": [-6.0, -5.0, -4.0, -3.0, -2.0],
    "speaker_boost_rapporteur_general": [0.04, 0.08, 0.12],  # 0.5x, 1.0x, 1.5x
    "speaker_boost_rapporteur": [0.03, 0.06, 0.09],
    "speaker_boost_ministre": [0.025, 0.05, 0.075],
    "speaker_boost_commission_pres": [0.02, 0.04, 0.06],
}


def main():
    golden_path = Path("data/eval/golden_set.json")
    if not golden_path.exists():
        print("Golden set not found. Run: python scripts/build_golden_set.py")
        sys.exit(1)

    cases = load_golden_set(golden_path)
    if not cases:
        print("No golden cases loaded")
        sys.exit(1)

    print(f"Loaded {len(cases)} golden cases")

    evaluator = PipelineEvaluator()

    # Baseline evaluation
    print("\n--- Baseline evaluation ---")
    baseline = evaluator.evaluate_search_quality(cases)
    print(f"  Recall@K: {baseline.recall_at_k:.3f}")
    print(f"  Precision@K: {baseline.precision_at_k:.3f}")
    print(f"  MRR: {baseline.mrr:.3f}")

    # Sweep each threshold
    all_results = {
        "baseline": {
            "recall": baseline.recall_at_k,
            "precision": baseline.precision_at_k,
            "mrr": baseline.mrr,
        },
        "sweeps": {},
    }

    for param_name, values in THRESHOLD_SWEEPS.items():
        print(f"\n--- Sweeping {param_name}: {values} ---")
        start = time.time()

        result = evaluator.compute_threshold_sensitivity(
            param_name, values, cases
        )

        elapsed = time.time() - start
        print(f"  Best value: {result.best_value} ({elapsed:.1f}s)")
        for m in result.metrics:
            print(
                f"  {param_name}={m['value']}: "
                f"recall={m['recall']:.3f} precision={m['precision']:.3f} mrr={m['mrr']:.3f}"
            )

        all_results["sweeps"][param_name] = {
            "values": values,
            "metrics": result.metrics,
            "best_value": result.best_value,
        }

    # Write results
    output_dir = Path("data/eval")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "sweep_results.json"

    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)

    print(f"\nResults written to {output_path}")


if __name__ == "__main__":
    main()
