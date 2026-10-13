#!/usr/bin/env python3
"""Step 2: Benchmark current OCR quality on sample PDFs.

Tests the Tesseract re-OCR output (data/reocr/) by parsing it through
the archive debate parser, computing quality metrics, and comparing
against the post-2002 baseline.

Usage:
    python scripts/ocr_pipeline/benchmark_ocr_quality.py
    python scripts/ocr_pipeline/benchmark_ocr_quality.py --samples-per-leg 3
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.parsers.archive_debates import (
    clean_archive_text,
    extract_date_from_text,
    extract_date_from_session_name,
    detect_archive_speaker_function,
    parse_archive_debate_from_text,
    _SPEAKER_PATTERN,
)
from src.parsers.debates import extract_article_mentions, Intervention, ParsedDebat

REOCR_DIR = Path("data/reocr")
RAW_DIR = Path("data/raw/archives_an")
OUTPUT_DIR = Path("data/ocr_pipeline/benchmark_results")
BASELINE_FILE = Path("data/ocr_pipeline/post2002_baseline.json")

LEGISLATURES = {
    1: "1958-1962",
    2: "1962-1967",
    3: "1967-1968",
    4: "1968-1973",
    5: "1973-1978",
    6: "1978-1981",
    7: "1981-1986",
    8: "1986-1988",
    9: "1988-1993",
    10: "1993-1997",
    11: "1997-2002",
}


def find_samples(samples_per_leg: int = 2) -> list[dict]:
    """Find sample body.txt files, picking from different sessions per legislature."""
    samples = []
    for leg_num in sorted(LEGISLATURES.keys()):
        leg_dir = REOCR_DIR / f"leg{leg_num}"
        if not leg_dir.exists():
            continue

        sessions = sorted([d for d in leg_dir.iterdir() if d.is_dir()])
        if not sessions:
            continue

        picked = 0
        for session_dir in sessions:
            if picked >= samples_per_leg:
                break
            body_files = sorted(session_dir.glob("*_body.txt"))
            if not body_files:
                continue
            # Pick a body file that has reasonable size (>1KB)
            for bf in body_files:
                if bf.stat().st_size > 1000:
                    pdf_name = bf.stem.replace("_body", "") + ".pdf"
                    pdf_path = RAW_DIR / f"leg{leg_num}" / session_dir.name / pdf_name
                    samples.append({
                        "legislature": leg_num,
                        "session": session_dir.name,
                        "body_file": bf,
                        "pdf_path": pdf_path,
                        "period": LEGISLATURES[leg_num],
                    })
                    picked += 1
                    break

    return samples


def parse_from_text(
    text: str, chambre: str, session_name: str, filename: str
) -> ParsedDebat | None:
    """Parse raw OCR text into a ParsedDebat. Delegates to shared parser."""
    return parse_archive_debate_from_text(
        text, chambre, session_name=session_name, filename=filename,
    )


def compute_metrics(results: list[dict]) -> dict:
    """Compute aggregate quality metrics from parsed results."""

    total = len(results)
    if total == 0:
        return {"error": "no results"}

    # Date extraction
    date_ok = sum(1 for r in results if r["date_extracted"])
    date_rate = round(100.0 * date_ok / total, 1)

    # Interventions
    intervention_counts = [r["intervention_count"] for r in results]
    intervention_lengths = []
    for r in results:
        intervention_lengths.extend(r["intervention_lengths"])

    # Speaker name format
    name_pattern = re.compile(r"^M(?:me|lle)?\.?\s+[A-ZÀ-Ý][a-zà-ÿ]")
    role_pattern = re.compile(r"^M(?:me)?\.?\s+l[ea]\s+")
    all_speakers = []
    for r in results:
        all_speakers.extend(r["speakers"])

    named_ok = sum(1 for s in all_speakers if name_pattern.match(s))
    role_ok = sum(1 for s in all_speakers if role_pattern.match(s))
    total_speakers = len(all_speakers)

    # Speaker name leak (function text in orateur field)
    leak_keywords = ["sur la situation", "concernant", "relative à", "portant sur"]
    speaker_no_leak = sum(
        1 for s in all_speakers
        if not any(kw in s.lower() for kw in leak_keywords) and len(s) < 80
    )

    # Function detection
    all_fonctions = []
    for r in results:
        all_fonctions.extend(r["fonctions"])
    non_autre = sum(1 for f in all_fonctions if f and f != "autre")

    # Articles
    debates_with_articles = sum(1 for r in results if r["has_articles"])

    # OCR artifact detection
    artifact_patterns = [
        r"[^\x00-\x7Fà-ÿÀ-Ý\s]",  # Non-French, non-ASCII chars
        r"\b[A-Z]{1}[a-z][A-Z]",  # Mixed case artifacts like "aRt"
    ]
    artifact_re = re.compile("|".join(artifact_patterns))
    clean_interventions = 0
    total_interventions = 0
    for r in results:
        for text in r["intervention_texts"]:
            total_interventions += 1
            if not artifact_re.search(text[:200]):
                clean_interventions += 1

    return {
        "total_samples": total,
        "date_extraction_rate": date_rate,
        "interventions_per_debate": {
            "mean": round(np.mean(intervention_counts), 1) if intervention_counts else 0,
            "median": round(float(np.median(intervention_counts)), 1) if intervention_counts else 0,
            "min": min(intervention_counts) if intervention_counts else 0,
            "max": max(intervention_counts) if intervention_counts else 0,
        },
        "avg_intervention_length": {
            "mean": round(np.mean(intervention_lengths), 1) if intervention_lengths else 0,
            "median": round(float(np.median(intervention_lengths)), 1) if intervention_lengths else 0,
        },
        "speaker_name_format": {
            "named_pct": round(100.0 * named_ok / total_speakers, 1) if total_speakers else 0,
            "role_based_pct": round(100.0 * role_ok / total_speakers, 1) if total_speakers else 0,
            "total_speakers": total_speakers,
        },
        "speaker_name_no_leak_pct": round(100.0 * speaker_no_leak / total_speakers, 1) if total_speakers else 0,
        "function_detection_rate": round(100.0 * non_autre / len(all_fonctions), 1) if all_fonctions else 0,
        "article_mention_rate": round(100.0 * debates_with_articles / total, 1),
        "contenu_coherence_pct": round(100.0 * clean_interventions / total_interventions, 1) if total_interventions else 0,
    }


def run_benchmark(samples_per_leg: int = 2):
    # Load baseline
    if not BASELINE_FILE.exists():
        print(f"ERROR: Run Step 1 first (baseline not found at {BASELINE_FILE})")
        sys.exit(1)

    with open(BASELINE_FILE) as f:
        baseline = json.load(f)

    # Find samples
    samples = find_samples(samples_per_leg)
    print(f"Found {len(samples)} samples across {len(LEGISLATURES)} legislatures\n")

    if not samples:
        print("ERROR: No sample files found in data/reocr/")
        sys.exit(1)

    # Process each sample
    results = []
    for s in samples:
        body_text = s["body_file"].read_text(encoding="utf-8", errors="replace")
        parsed = parse_from_text(
            body_text,
            chambre="AN",
            session_name=s["session"],
            filename=s["body_file"].stem.replace("_body", ""),
        )

        result = {
            "legislature": s["legislature"],
            "session": s["session"],
            "file": s["body_file"].name,
            "period": s["period"],
            "text_length": len(body_text),
            "date_extracted": parsed is not None and parsed.date is not None,
            "date": parsed.date.isoformat() if parsed and parsed.date else None,
            "intervention_count": len(parsed.interventions) if parsed else 0,
            "intervention_lengths": [len(i.contenu) for i in parsed.interventions] if parsed else [],
            "speakers": [i.orateur for i in parsed.interventions] if parsed else [],
            "fonctions": [i.fonction for i in parsed.interventions] if parsed else [],
            "has_articles": any(i.articles_mentionnes for i in parsed.interventions) if parsed else False,
            "intervention_texts": [i.contenu for i in parsed.interventions] if parsed else [],
        }
        results.append(result)

        status = "OK" if parsed and len(parsed.interventions) > 2 else "POOR"
        print(f"  leg{s['legislature']:2d} | {s['session']:30s} | {s['body_file'].name:15s} | "
              f"interventions={result['intervention_count']:3d} | "
              f"date={'YES' if result['date_extracted'] else 'NO ':3s} | {status}")

    # Compute metrics
    metrics = compute_metrics(results)

    # Compare with baseline
    bl_ipd = baseline["debate_stats"]["interventions_per_debate"]
    bl_cl = baseline["intervention_stats"]["contenu_length"]
    bl_snf = baseline["intervention_stats"]["speaker_name_format"]

    print("\n" + "=" * 70)
    print("QUALITY COMPARISON: Tesseract re-OCR vs Post-2002 baseline")
    print("=" * 70)

    comparisons = [
        ("Date extraction rate", f"{metrics['date_extraction_rate']}%", ">= 98%"),
        ("Interventions/debate (mean)", str(metrics["interventions_per_debate"]["mean"]), str(bl_ipd["mean"])),
        ("Interventions/debate (median)", str(metrics["interventions_per_debate"]["median"]), str(bl_ipd["p50"])),
        ("Intervention length (mean)", str(metrics["avg_intervention_length"]["mean"]), str(bl_cl["mean"])),
        ("Speaker named format", f"{metrics['speaker_name_format']['named_pct']}%", f"{bl_snf['named_pct']}%"),
        ("Speaker no leak", f"{metrics['speaker_name_no_leak_pct']}%", ">= 95%"),
        ("Function detection", f"{metrics['function_detection_rate']}%", ">= 80%"),
        ("Article mentions", f"{metrics['article_mention_rate']}%", f"{baseline['intervention_stats']['has_articles_mentionnes_pct']}%"),
        ("Content coherence", f"{metrics['contenu_coherence_pct']}%", ">= 90%"),
    ]

    print(f"\n{'Metric':<35s} {'Current':>12s} {'Target':>12s}")
    print("-" * 60)
    for name, current, target in comparisons:
        print(f"{name:<35s} {current:>12s} {target:>12s}")

    # Save results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = OUTPUT_DIR / f"tesseract_baseline_{timestamp}.json"

    # Strip large text fields for JSON output
    results_clean = []
    for r in results:
        r_clean = {k: v for k, v in r.items() if k != "intervention_texts"}
        results_clean.append(r_clean)

    output = {
        "generated_at": datetime.now().isoformat(),
        "source": "data/reocr/ (Tesseract 5 re-OCR + fusion)",
        "samples_per_legislature": samples_per_leg,
        "metrics": metrics,
        "per_sample": results_clean,
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nResults saved to {output_file}")

    # Also save sample texts for shootout comparison (Step 3)
    texts_dir = OUTPUT_DIR / "sample_texts"
    texts_dir.mkdir(exist_ok=True)
    for s in samples:
        dest = texts_dir / f"leg{s['legislature']}_{s['session']}_{s['body_file'].stem}.txt"
        body_text = s["body_file"].read_text(encoding="utf-8", errors="replace")
        dest.write_text(body_text, encoding="utf-8")

    print(f"Sample texts saved to {texts_dir}/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark OCR quality on samples")
    parser.add_argument("--samples-per-leg", type=int, default=2,
                        help="Number of samples per legislature (default: 2)")
    args = parser.parse_args()
    run_benchmark(args.samples_per_leg)
