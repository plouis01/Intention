#!/usr/bin/env python3
"""Step 4: Run OCR on archive PDFs using the chosen engine(s).

Processes PDF files page by page, extracting text via a local VLM model
through Ollama. Supports single PDF, legislature, or full batch mode.

Usage:
    python scripts/ocr_pipeline/run_ocr.py --engine glm-ocr --pdf data/raw/archives_an/leg1/.../001.pdf
    python scripts/ocr_pipeline/run_ocr.py --engine glm-ocr --legislature 1
    python scripts/ocr_pipeline/run_ocr.py --engine glm-ocr --all
    python scripts/ocr_pipeline/run_ocr.py --engine glm-ocr --legislature 1 --resume
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.ocr.engines.ollama_vlm import OllamaVLMEngine
from src.ocr.engines.pdf_to_images import pdf_page_to_image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

RAW_AN_DIR = Path("data/raw/archives_an")
RAW_SENAT_DIR = Path("data/raw/archives_senat")
OUTPUT_BASE = Path("data/ocr_output")

LEGISLATURES = list(range(1, 12))  # 1-11


def discover_pdfs(legislature: int | None = None, chambre: str = "an") -> list[Path]:
    """Discover PDF files to process."""
    if chambre == "an":
        base = RAW_AN_DIR
        if legislature:
            base = base / f"leg{legislature}"
    else:
        base = RAW_SENAT_DIR

    if not base.exists():
        logger.error(f"Directory not found: {base}")
        return []

    pdfs = sorted(base.rglob("*.pdf"))
    logger.info(f"Found {len(pdfs)} PDFs in {base}")
    return pdfs


def output_path_for_pdf(pdf_path: Path, engine: str, chambre: str = "an") -> Path:
    """Compute output text file path for a given PDF."""
    if chambre == "an":
        # data/raw/archives_an/leg1/session/001.pdf
        # -> data/ocr_output/glm-ocr/an/leg1/session/001.txt
        rel = pdf_path.relative_to(RAW_AN_DIR)
    else:
        rel = pdf_path.relative_to(RAW_SENAT_DIR)

    return OUTPUT_BASE / engine / chambre / rel.with_suffix(".txt")


def process_pdf(
    pdf_path: Path,
    engine: OllamaVLMEngine,
    output_file: Path,
    dpi: int = 300,
) -> dict:
    """Process a single PDF through the OCR engine.

    Returns stats dict.
    """
    import fitz

    doc = fitz.open(pdf_path)
    page_count = len(doc)
    doc.close()

    pages_text = []
    total_chars = 0
    total_time = 0.0
    errors = 0

    for page_num in range(page_count):
        start = time.time()
        try:
            text = engine.ocr_pdf_page(pdf_path, page_num, dpi=dpi)
            elapsed = time.time() - start
            pages_text.append(text)
            total_chars += len(text)
            total_time += elapsed
        except Exception as e:
            elapsed = time.time() - start
            total_time += elapsed
            errors += 1
            pages_text.append("")
            logger.warning(f"  Page {page_num} failed: {e}")

    # Write output
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Write with page separators
    full_text = "\n\n".join(pages_text)
    output_file.write_text(full_text, encoding="utf-8")

    return {
        "pdf": str(pdf_path),
        "pages": page_count,
        "total_chars": total_chars,
        "total_time": round(total_time, 1),
        "avg_time_per_page": round(total_time / max(page_count, 1), 1),
        "errors": errors,
    }


def run_ocr(
    engine_name: str,
    legislature: int | None = None,
    pdf_path: str | None = None,
    chambre: str = "an",
    dpi: int = 300,
    resume: bool = False,
    all_legs: bool = False,
):
    """Run OCR on specified PDFs."""

    engine = OllamaVLMEngine(model=engine_name)

    # Determine PDFs to process
    if pdf_path:
        pdfs = [Path(pdf_path)]
    elif all_legs:
        pdfs = []
        for leg in LEGISLATURES:
            pdfs.extend(discover_pdfs(legislature=leg, chambre=chambre))
    elif legislature:
        pdfs = discover_pdfs(legislature=legislature, chambre=chambre)
    else:
        logger.error("Specify --pdf, --legislature N, or --all")
        sys.exit(1)

    if not pdfs:
        logger.error("No PDFs found")
        sys.exit(1)

    # Filter already processed (resume mode)
    if resume:
        pending = []
        for p in pdfs:
            out = output_path_for_pdf(p, engine_name, chambre)
            if not out.exists() or out.stat().st_size == 0:
                pending.append(p)
        skipped = len(pdfs) - len(pending)
        if skipped > 0:
            logger.info(f"Resume: skipping {skipped} already processed PDFs")
        pdfs = pending

    logger.info(f"Processing {len(pdfs)} PDFs with {engine_name} (DPI={dpi})")

    # Process
    stats = []
    log_file = OUTPUT_BASE / engine_name / f"{chambre}_processing.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    for i, pdf in enumerate(pdfs):
        out = output_path_for_pdf(pdf, engine_name, chambre)
        logger.info(f"[{i + 1}/{len(pdfs)}] {pdf.name}")

        result = process_pdf(pdf, engine, out, dpi=dpi)
        stats.append(result)

        logger.info(
            f"  {result['pages']} pages, {result['total_chars']} chars, "
            f"{result['total_time']}s ({result['avg_time_per_page']}s/page)"
        )

        # Append to log
        with open(log_file, "a") as f:
            f.write(json.dumps(result, default=str) + "\n")

    # Summary
    if stats:
        total_pages = sum(s["pages"] for s in stats)
        total_chars = sum(s["total_chars"] for s in stats)
        total_time = sum(s["total_time"] for s in stats)
        total_errors = sum(s["errors"] for s in stats)

        logger.info(f"\n{'='*60}")
        logger.info(f"DONE: {len(stats)} PDFs, {total_pages} pages, {total_chars} chars")
        logger.info(f"Time: {total_time:.0f}s ({total_time/max(total_pages,1):.1f}s/page)")
        if total_errors:
            logger.warning(f"Errors: {total_errors} pages failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run OCR on archive PDFs")
    parser.add_argument("--engine", type=str, required=True,
                        help="OCR engine name (glm-ocr, qwen3-vl, etc.)")
    parser.add_argument("--pdf", type=str, help="Process a single PDF file")
    parser.add_argument("--legislature", type=int, help="Process a single legislature (1-11)")
    parser.add_argument("--all", action="store_true", help="Process all legislatures")
    parser.add_argument("--chambre", type=str, default="an", choices=["an", "senat"],
                        help="Chamber (default: an)")
    parser.add_argument("--dpi", type=int, default=300, help="DPI for rendering (default: 300)")
    parser.add_argument("--resume", action="store_true", help="Skip already processed PDFs")
    args = parser.parse_args()

    run_ocr(
        engine_name=args.engine,
        legislature=args.legislature,
        pdf_path=args.pdf,
        chambre=args.chambre,
        dpi=args.dpi,
        resume=args.resume,
        all_legs=args.all,
    )
