#!/usr/bin/env python3
"""Validate the full OCR → parsing pipeline on a sample of archive PDFs.

Runs Google Document AI OCR on 2-3 PDFs per legislature, parses the output
through the archive debate parser, computes quality metrics, and compares
against the post-2002 baseline.

Prerequisites:
    pip install -e ".[google-ocr]"
    python scripts/ocr_pipeline/setup_google_docai.py

Usage:
    python scripts/ocr_pipeline/validate_full_pipeline.py --samples-per-leg 2
    python scripts/ocr_pipeline/validate_full_pipeline.py --samples-per-leg 3 --save-ocr-text
    python scripts/ocr_pipeline/validate_full_pipeline.py --legislatures 1,7,11
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.ocr.engines.google_docai import GoogleDocumentAIEngine
from src.parsers.archive_debates import parse_archive_debate_from_text
from src.parsers.debates import ParsedDebat

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

RAW_DIR = Path("data/raw/archives_an")
OUTPUT_DIR = Path("data/ocr_pipeline/validation_results")
BASELINE_FILE = Path("data/ocr_pipeline/post2002_baseline.json")

LEGISLATURES = {
    1: "1958-1962", 2: "1962-1967", 3: "1967-1968", 4: "1968-1973",
    5: "1973-1978", 6: "1978-1981", 7: "1981-1986", 8: "1986-1988",
    9: "1988-1993", 10: "1993-1997", 11: "1997-2002",
}


def select_samples(
    samples_per_leg: int = 2,
    legislatures: list[int] | None = None,
) -> list[dict]:
    """Select sample PDFs: N per legislature, from different sessions."""
    import fitz

    target_legs = sorted(legislatures or LEGISLATURES.keys())
    samples = []

    for leg_num in target_legs:
        leg_dir = RAW_DIR / f"leg{leg_num}"
        if not leg_dir.exists():
            print(f"  SKIP leg{leg_num}: directory not found")
            continue

        sessions = sorted(d for d in leg_dir.iterdir() if d.is_dir())
        if not sessions:
            continue

        # Pick from start, middle, end of session list
        indices = [0, len(sessions) // 2, len(sessions) - 1][:samples_per_leg]
        # Deduplicate (small session lists)
        indices = sorted(set(indices))

        picked = 0
        for idx in indices:
            if picked >= samples_per_leg:
                break
            session_dir = sessions[idx]

            for pdf_path in sorted(session_dir.glob("*.pdf")):
                doc = fitz.open(pdf_path)
                page_count = len(doc)
                doc.close()

                if 5 <= page_count <= 50:
                    samples.append({
                        "legislature": leg_num,
                        "session": session_dir.name,
                        "pdf_path": pdf_path,
                        "pdf_name": pdf_path.name,
                        "page_count": page_count,
                        "period": LEGISLATURES[leg_num],
                    })
                    picked += 1
                    break

    return samples


def ocr_pdf_docai(engine: GoogleDocumentAIEngine, pdf_path: Path) -> tuple[str, float]:
    """OCR all pages of a PDF via Google Document AI, page by page.

    Returns:
        (combined_text, elapsed_seconds)
    """
    import fitz

    doc = fitz.open(pdf_path)
    page_count = len(doc)
    doc.close()

    pages_text = []
    start = time.time()

    for page_num in range(page_count):
        text = engine.ocr_pdf_page(pdf_path, page_num)
        pages_text.append(text)

    elapsed = time.time() - start
    return "\n\n".join(pages_text), elapsed


def compute_metrics(results: list[dict], baseline: dict) -> dict:
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

    # Speaker name format: M(me)?. Name
    name_pattern = re.compile(r"^M(?:me|lle)?\.?\s+[A-ZÀ-Ý][a-zà-ÿ]")
    role_pattern = re.compile(r"^M(?:me)?\.?\s+l[ea]\s+")
    all_speakers = []
    for r in results:
        all_speakers.extend(r["speakers"])

    total_speakers = len(all_speakers)
    named_ok = sum(1 for s in all_speakers if name_pattern.match(s))
    role_ok = sum(1 for s in all_speakers if role_pattern.match(s))
    # Both named and role-based are valid speaker formats
    valid_format = named_ok + role_ok

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

    # OCR artifact detection (allow French accents, quotes, dashes, ordinals, degree)
    artifact_re = re.compile(r"[^\x00-\x7Fà-ÿÀ-Ý\s°ªº«»–—''""…€œŒæÆ]")
    clean_interventions = 0
    total_interventions = 0
    for r in results:
        for text in r["intervention_texts"]:
            total_interventions += 1
            if not artifact_re.search(text[:200]):
                clean_interventions += 1

    # Broken hyphenation check (should be 0 after parser fix)
    broken_hyphens = 0
    for r in results:
        for text in r["intervention_texts"]:
            broken_hyphens += len(re.findall(r"\w+-\n[a-z]", text))

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
            "valid_pct": round(100.0 * valid_format / total_speakers, 1) if total_speakers else 0,
            "named_pct": round(100.0 * named_ok / total_speakers, 1) if total_speakers else 0,
            "role_based_pct": round(100.0 * role_ok / total_speakers, 1) if total_speakers else 0,
            "total_speakers": total_speakers,
        },
        "speaker_name_no_leak_pct": round(100.0 * speaker_no_leak / total_speakers, 1) if total_speakers else 0,
        "function_detection_rate": round(100.0 * non_autre / len(all_fonctions), 1) if all_fonctions else 0,
        "article_mention_rate": round(100.0 * debates_with_articles / total, 1),
        "contenu_coherence_pct": round(100.0 * clean_interventions / total_interventions, 1) if total_interventions else 0,
        "broken_hyphens_total": broken_hyphens,
    }


def print_report(metrics: dict, baseline: dict, per_leg: dict[int, dict]):
    """Print comparison report to stdout."""
    bl_ipd = baseline["debate_stats"]["interventions_per_debate"]
    bl_cl = baseline["intervention_stats"]["contenu_length"]

    print("\n" + "=" * 75)
    print("FULL PIPELINE VALIDATION — Google DocAI + Parser")
    print("=" * 75)

    comparisons = [
        ("Date extraction rate", f"{metrics['date_extraction_rate']}%", ">= 98%"),
        ("Interventions/debate (mean)", str(metrics["interventions_per_debate"]["mean"]), f"{bl_ipd['mean']} (ref)"),
        ("Interventions/debate (median)", str(metrics["interventions_per_debate"]["median"]), f"{bl_ipd['p50']} (ref)"),
        ("Intervention length (mean)", str(metrics["avg_intervention_length"]["mean"]), f"{bl_cl['mean']} (ref)"),
        ("Speaker valid format", f"{metrics['speaker_name_format']['valid_pct']}%", ">= 90%"),
        ("Speaker no leak", f"{metrics['speaker_name_no_leak_pct']}%", ">= 95%"),
        ("Function detection", f"{metrics['function_detection_rate']}%", ">= 30%"),
        ("Article mentions", f"{metrics['article_mention_rate']}%", ">= 20%"),
        ("Content coherence", f"{metrics['contenu_coherence_pct']}%", ">= 90%"),
        ("Broken hyphens (total)", str(metrics["broken_hyphens_total"]), "0"),
    ]

    print(f"\n{'Metric':<35s} {'Current':>12s} {'Target':>15s}")
    print("-" * 65)
    for name, current, target in comparisons:
        print(f"{name:<35s} {current:>12s} {target:>15s}")

    # Per-legislature breakdown
    print(f"\n{'Leg':<5s} {'Period':<12s} {'Samples':>8s} {'Date%':>7s} {'Intv/deb':>9s} {'Spk fmt%':>9s} {'Func%':>7s} {'Coher%':>8s}")
    print("-" * 72)
    for leg_num in sorted(per_leg.keys()):
        m = per_leg[leg_num]
        print(
            f"{leg_num:<5d} {LEGISLATURES.get(leg_num, '?'):<12s} "
            f"{m['total_samples']:>8d} "
            f"{m['date_extraction_rate']:>6.0f}% "
            f"{m['interventions_per_debate']['mean']:>9.1f} "
            f"{m['speaker_name_format']['valid_pct']:>8.1f}% "
            f"{m['function_detection_rate']:>6.1f}% "
            f"{m['contenu_coherence_pct']:>7.1f}%"
        )


def run_validation(
    samples_per_leg: int = 2,
    legislatures: list[int] | None = None,
    save_ocr_text: bool = False,
):
    """Run the full validation pipeline."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load baseline
    if not BASELINE_FILE.exists():
        print(f"WARNING: Baseline not found at {BASELINE_FILE}, skipping comparison")
        baseline = {}
    else:
        with open(BASELINE_FILE) as f:
            baseline = json.load(f)

    # Initialize Google DocAI
    print("Initializing Google Document AI...")
    engine = GoogleDocumentAIEngine()
    print(f"  Project: {engine.project_id}, Processor: {engine.processor_id}\n")

    # Select samples
    print("Selecting sample PDFs...")
    samples = select_samples(samples_per_leg, legislatures)
    total_pages = sum(s["page_count"] for s in samples)
    print(f"  {len(samples)} PDFs selected, {total_pages} total pages\n")

    if not samples:
        print("ERROR: No sample PDFs found")
        sys.exit(1)

    # OCR text dir
    ocr_text_dir = OUTPUT_DIR / "ocr_text"
    if save_ocr_text:
        ocr_text_dir.mkdir(parents=True, exist_ok=True)

    # Process each sample
    results = []
    total_ocr_time = 0.0

    for i, s in enumerate(samples):
        pdf_path = s["pdf_path"]
        label = f"leg{s['legislature']}_{s['session']}_{s['pdf_name']}"
        print(f"[{i+1}/{len(samples)}] {label} ({s['page_count']} pages)...")

        # OCR
        ocr_text, ocr_time = ocr_pdf_docai(engine, pdf_path)
        total_ocr_time += ocr_time
        print(f"  OCR: {len(ocr_text):,d} chars in {ocr_time:.1f}s ({s['page_count'] / max(ocr_time, 0.1):.1f} pages/s)")

        if save_ocr_text:
            (ocr_text_dir / f"{label}.txt").write_text(ocr_text, encoding="utf-8")

        # Parse
        parsed = parse_archive_debate_from_text(
            ocr_text, chambre="AN",
            session_name=s["session"], filename=s["pdf_name"].replace(".pdf", ""),
        )

        result = {
            "legislature": s["legislature"],
            "session": s["session"],
            "pdf": s["pdf_name"],
            "period": s["period"],
            "page_count": s["page_count"],
            "ocr_chars": len(ocr_text),
            "ocr_time": round(ocr_time, 2),
            "date_extracted": parsed is not None and parsed.date is not None,
            "date": parsed.date.isoformat() if parsed and parsed.date else None,
            "intervention_count": len(parsed.interventions) if parsed else 0,
            "intervention_lengths": [len(iv.contenu) for iv in parsed.interventions] if parsed else [],
            "speakers": [iv.orateur for iv in parsed.interventions] if parsed else [],
            "fonctions": [iv.fonction for iv in parsed.interventions] if parsed else [],
            "has_articles": any(iv.articles_mentionnes for iv in parsed.interventions) if parsed else False,
            "intervention_texts": [iv.contenu for iv in parsed.interventions] if parsed else [],
        }
        results.append(result)

        n_intv = result["intervention_count"]
        status = "OK" if parsed and n_intv > 5 else "POOR" if parsed else "FAIL"
        print(f"  Parse: {n_intv} interventions, date={'YES' if result['date_extracted'] else 'NO'} [{status}]")

    # Compute metrics
    print(f"\nTotal OCR time: {total_ocr_time:.1f}s for {total_pages} pages ({total_pages / max(total_ocr_time, 0.1):.1f} pages/s)")

    metrics = compute_metrics(results, baseline)

    # Per-legislature metrics
    per_leg = {}
    for leg_num in sorted(set(r["legislature"] for r in results)):
        leg_results = [r for r in results if r["legislature"] == leg_num]
        per_leg[leg_num] = compute_metrics(leg_results, baseline)

    # Print report
    if baseline:
        print_report(metrics, baseline, per_leg)

    # Save results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = OUTPUT_DIR / f"docai_validation_{timestamp}.json"

    # Strip large text fields
    results_clean = []
    for r in results:
        r_clean = {k: v for k, v in r.items() if k != "intervention_texts"}
        results_clean.append(r_clean)

    report = {
        "generated_at": datetime.now().isoformat(),
        "engine": "google-docai",
        "samples_per_legislature": samples_per_leg,
        "total_pages": total_pages,
        "total_ocr_time_seconds": round(total_ocr_time, 1),
        "metrics": metrics,
        "per_legislature": {str(k): v for k, v in per_leg.items()},
        "per_sample": results_clean,
    }

    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nFull report saved to {report_file}")

    # Print worst samples for debugging
    print("\n--- Worst samples (fewest interventions) ---")
    for r in sorted(results, key=lambda x: x["intervention_count"])[:5]:
        print(f"  leg{r['legislature']} {r['session']}/{r['pdf']} : {r['intervention_count']} interventions, {r['ocr_chars']:,d} chars")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate full OCR pipeline")
    parser.add_argument("--samples-per-leg", type=int, default=2,
                        help="Number of PDFs per legislature (default: 2)")
    parser.add_argument("--legislatures", type=str, default=None,
                        help="Comma-separated legislature numbers (default: all)")
    parser.add_argument("--save-ocr-text", action="store_true",
                        help="Save raw OCR text per PDF for debugging")
    args = parser.parse_args()

    legs = None
    if args.legislatures:
        legs = [int(x.strip()) for x in args.legislatures.split(",")]

    run_validation(args.samples_per_leg, legs, args.save_ocr_text)
