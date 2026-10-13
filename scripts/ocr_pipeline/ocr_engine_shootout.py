#!/usr/bin/env python3
"""Step 3: OCR Engine Shootout — compare OCR models on sample pages.

Tests multiple OCR models (local via Ollama, or cloud via Google Document AI)
on identical sample pages from different decades, measures quality metrics,
and produces a comparison report to select the best engine.

Prerequisites (Ollama models):
    brew install ollama  (or download from ollama.com)
    ollama serve  (start the server)
    ollama pull glm-ocr

Prerequisites (Google Document AI):
    python scripts/ocr_pipeline/setup_google_docai.py
    pip install -e ".[google-ocr]"

Usage:
    python scripts/ocr_pipeline/ocr_engine_shootout.py --list-models
    python scripts/ocr_pipeline/ocr_engine_shootout.py --models glm-ocr,google-docai
    python scripts/ocr_pipeline/ocr_engine_shootout.py --models google-docai
"""

import argparse
import json
import sys
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.ocr.engines.ollama_vlm import OllamaVLMEngine
from src.ocr.engines.pdf_to_images import pdf_page_to_image

RAW_DIR = Path("data/raw/archives_an")
OUTPUT_DIR = Path("data/ocr_pipeline/shootout_results")
GROUND_TRUTH_DIR = Path("data/ocr_pipeline/ground_truth")

# Sample pages to test: diverse decades and quality levels
# Format: (legislature, session, pdf_name, page_numbers, decade_label)
SAMPLE_PAGES = [
    (1, "1958-1959-ordinaire1", "001.pdf", [2, 3], "1958-1960s"),
    (3, "1967-1968-ordinaire1", "001.pdf", [2, 3], "1967-1970s"),
    (5, "1973-1974-extraordinaire1", "001.pdf", [2, 3], "1973-1970s"),
    (7, "1981-1982-ordinaire1", "001.pdf", [2, 3], "1981-1980s"),
    (9, "1988-1989-ordinaire1", "001.pdf", [2, 3], "1988-1990s"),
    (11, "1997-1998-ordinaire1", "001.pdf", [2, 3], "1997-2000s"),
]

AVAILABLE_MODELS = ["glm-ocr", "lightonocr-2", "qwen3-vl", "olmocr", "google-docai"]

# Models that use cloud APIs (not Ollama)
CLOUD_MODELS = {"google-docai"}


def get_sample_images(dpi: int = 300) -> list[dict]:
    """Generate sample page images from PDFs."""
    images = []
    images_dir = OUTPUT_DIR / "sample_images"
    images_dir.mkdir(parents=True, exist_ok=True)

    for leg, session, pdf_name, page_nums, decade in SAMPLE_PAGES:
        pdf_path = RAW_DIR / f"leg{leg}" / session / pdf_name
        if not pdf_path.exists():
            print(f"  SKIP: {pdf_path} not found")
            continue

        for page_num in page_nums:
            image_name = f"leg{leg}_{session}_{pdf_name.replace('.pdf', '')}_p{page_num}.png"
            image_path = images_dir / image_name

            if not image_path.exists():
                print(f"  Rendering {pdf_path.name} page {page_num}...")
                pdf_page_to_image(pdf_path, page_num, dpi=dpi, output_path=image_path)

            images.append({
                "image_path": image_path,
                "pdf_path": pdf_path,
                "legislature": leg,
                "session": session,
                "pdf": pdf_name,
                "page": page_num,
                "decade": decade,
                "label": f"leg{leg}_p{page_num}",
            })

    return images


def _create_engine(model_name: str):
    """Create the appropriate OCR engine for a model name."""
    if model_name == "google-docai":
        from src.ocr.engines.google_docai import GoogleDocumentAIEngine
        return GoogleDocumentAIEngine()
    return OllamaVLMEngine(model=model_name)


def run_model_on_images(model_name: str, images: list[dict]) -> list[dict]:
    """Run a single model on all sample images."""
    engine = _create_engine(model_name)

    use_pdf = model_name in CLOUD_MODELS

    results = []
    for img in images:
        start = time.time()
        try:
            if use_pdf:
                text = engine.ocr_pdf_page(img["pdf_path"], img["page"])
            else:
                text = engine.ocr_image(img["image_path"])
            elapsed = time.time() - start
            success = True
        except Exception as e:
            text = ""
            elapsed = time.time() - start
            success = False
            print(f"    ERROR on {img['label']}: {e}")

        results.append({
            "label": img["label"],
            "decade": img["decade"],
            "text": text,
            "text_length": len(text),
            "elapsed_seconds": round(elapsed, 2),
            "success": success,
        })

        status = f"{len(text):5d} chars" if success else "FAILED"
        print(f"    {img['label']:20s} | {status} | {elapsed:.1f}s")

    return results


def compute_quality_metrics(text: str, ground_truth: str | None = None) -> dict:
    """Compute quality metrics for OCR output."""
    metrics = {
        "char_count": len(text),
        "word_count": len(text.split()),
        "line_count": text.count("\n") + 1,
    }

    # French accent presence (good OCR preserves accents)
    accent_chars = set("éèêëàâùûüôîïçÉÈÊËÀÂÙÛÜÔÎÏÇ")
    accent_count = sum(1 for c in text if c in accent_chars)
    metrics["accent_count"] = accent_count
    metrics["accent_density"] = round(accent_count / max(len(text), 1) * 1000, 2)

    # Speaker pattern detection
    import re
    speaker_pattern = re.compile(r"M(?:me|lle)?\.?\s+[A-ZÀ-Ý][a-zà-ÿ]")
    speakers_found = len(speaker_pattern.findall(text))
    metrics["speakers_found"] = speakers_found

    # Garbage character ratio
    garbage_chars = sum(1 for c in text if ord(c) > 127 and c not in accent_chars and c not in "—–«»''""…°€")
    metrics["garbage_char_ratio"] = round(garbage_chars / max(len(text), 1) * 100, 2)

    # If ground truth available, compute CER/WER
    if ground_truth:
        # Character Error Rate
        sm = SequenceMatcher(None, ground_truth, text)
        cer = 1.0 - sm.ratio()
        metrics["cer"] = round(cer * 100, 2)

        # Word Error Rate
        gt_words = ground_truth.split()
        ocr_words = text.split()
        sm_words = SequenceMatcher(None, gt_words, ocr_words)
        wer = 1.0 - sm_words.ratio()
        metrics["wer"] = round(wer * 100, 2)

        # Accent accuracy
        gt_accents = [(i, c) for i, c in enumerate(ground_truth) if c in accent_chars]
        if gt_accents:
            # Check how many accented chars in GT appear in OCR
            matched = sum(1 for _, c in gt_accents if c in text)
            metrics["accent_recall"] = round(100.0 * matched / len(gt_accents), 1)

    return metrics


def generate_report(all_results: dict[str, list[dict]], images: list[dict]):
    """Generate comparison report."""
    print("\n" + "=" * 80)
    print("OCR ENGINE SHOOTOUT — COMPARISON REPORT")
    print("=" * 80)

    # Summary table
    print(f"\n{'Model':<20s} {'Avg chars':>10s} {'Avg time':>10s} {'Accents':>8s} {'Speakers':>9s} {'Garbage':>8s}")
    print("-" * 70)

    model_scores = {}

    for model, results in all_results.items():
        successful = [r for r in results if r["success"]]
        if not successful:
            print(f"{model:<20s} {'FAILED':>10s}")
            continue

        avg_chars = sum(r["text_length"] for r in successful) / len(successful)
        avg_time = sum(r["elapsed_seconds"] for r in successful) / len(successful)

        # Compute metrics on all results
        all_metrics = []
        for r in successful:
            m = compute_quality_metrics(r["text"])
            all_metrics.append(m)

        avg_accents = sum(m["accent_density"] for m in all_metrics) / len(all_metrics)
        avg_speakers = sum(m["speakers_found"] for m in all_metrics) / len(all_metrics)
        avg_garbage = sum(m["garbage_char_ratio"] for m in all_metrics) / len(all_metrics)

        print(f"{model:<20s} {avg_chars:>10.0f} {avg_time:>9.1f}s {avg_accents:>7.1f}‰ {avg_speakers:>8.1f} {avg_garbage:>7.2f}%")

        # Composite score: more chars (good), more accents (good), less garbage (good), more speakers (good)
        score = (
            min(avg_chars / 3000, 1.0) * 30  # Normalize to ~3000 chars/page
            + min(avg_accents / 20, 1.0) * 25  # ~20 accents per 1000 chars expected
            + max(0, 1.0 - avg_garbage / 5) * 25  # Penalize garbage > 5%
            + min(avg_speakers / 5, 1.0) * 20  # ~5 speakers per page expected
        )
        model_scores[model] = round(score, 1)

    # Ranking
    print(f"\n{'Model':<20s} {'Score':>8s}")
    print("-" * 30)
    for model, score in sorted(model_scores.items(), key=lambda x: -x[1]):
        marker = " <-- BEST" if score == max(model_scores.values()) else ""
        print(f"{model:<20s} {score:>7.1f}{marker}")

    return model_scores


def run_shootout(models: list[str], dpi: int = 300):
    """Run the full shootout."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    ollama_models = [m for m in models if m not in CLOUD_MODELS]
    cloud_models = [m for m in models if m in CLOUD_MODELS]

    # Check Ollama for local models
    if ollama_models:
        available = OllamaVLMEngine.list_available()
        if not available:
            print("ERROR: Ollama is not running or no models available.")
            print("Start Ollama with: ollama serve")
            print("Then pull models with: ollama pull glm-ocr")
            sys.exit(1)

        print(f"Ollama models available: {available}")
        for m in ollama_models:
            tag = OllamaVLMEngine(model=m).model_tag
            if not any(tag in a for a in available):
                print(f"WARNING: Model '{m}' (tag: {tag}) not found in Ollama.")
                print(f"  Pull it with: ollama pull {tag}")

    if cloud_models:
        print(f"Cloud models: {cloud_models}")

    print(f"Models to test: {models}\n")

    # Generate sample images
    print("Preparing sample images...")
    images = get_sample_images(dpi=dpi)
    print(f"  {len(images)} sample pages ready\n")

    if not images:
        print("ERROR: No sample images generated. Check that PDFs exist in data/raw/archives_an/")
        sys.exit(1)

    # Run each model
    all_results = {}
    for model in models:
        print(f"\n--- Running {model} ---")
        results = run_model_on_images(model, images)
        all_results[model] = results

        # Save individual results
        model_dir = OUTPUT_DIR / model
        model_dir.mkdir(exist_ok=True)
        for r in results:
            text_file = model_dir / f"{r['label']}.txt"
            text_file.write_text(r["text"], encoding="utf-8")

    # Generate report
    scores = generate_report(all_results, images)

    # Save full results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = OUTPUT_DIR / f"comparison_report_{timestamp}.json"

    # Strip large text fields
    results_for_json = {}
    for model, results in all_results.items():
        results_for_json[model] = [
            {k: v for k, v in r.items() if k != "text"}
            for r in results
        ]

    report = {
        "generated_at": datetime.now().isoformat(),
        "models_tested": models,
        "dpi": dpi,
        "sample_count": len(images),
        "scores": scores,
        "results": results_for_json,
    }

    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nFull report saved to {report_file}")

    # Markdown report
    md_file = OUTPUT_DIR / f"comparison_report_{timestamp}.md"
    with open(md_file, "w", encoding="utf-8") as f:
        f.write("# OCR Engine Shootout Results\n\n")
        f.write(f"Date: {datetime.now().isoformat()}\n")
        f.write(f"Models: {', '.join(models)}\n")
        f.write(f"Samples: {len(images)} pages\n\n")
        f.write("## Scores\n\n")
        f.write("| Model | Score |\n|-------|-------|\n")
        for model, score in sorted(scores.items(), key=lambda x: -x[1]):
            f.write(f"| {model} | {score} |\n")
    print(f"Markdown report saved to {md_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OCR Engine Shootout")
    parser.add_argument("--models", type=str, default="glm-ocr",
                        help="Comma-separated model names (default: glm-ocr)")
    parser.add_argument("--list-models", action="store_true",
                        help="List available Ollama models and exit")
    parser.add_argument("--dpi", type=int, default=150,
                        help="DPI for PDF rendering (default: 150, optimal for VLMs)")
    args = parser.parse_args()

    if args.list_models:
        available = OllamaVLMEngine.list_available()
        print("Available Ollama models:")
        for m in available:
            print(f"  {m}")
        print(f"\nCloud models: {sorted(CLOUD_MODELS)}")
        print(f"All supported: {AVAILABLE_MODELS}")
        sys.exit(0)

    models = [m.strip() for m in args.models.split(",")]
    run_shootout(models, dpi=args.dpi)
