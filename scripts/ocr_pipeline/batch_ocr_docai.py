#!/usr/bin/env python3
"""Batch OCR processing of archive PDFs using Google Document AI.

Processes archive PDFs (AN legislatures 1-11 or Sénat 1958-1996) through
Document AI OCR, saving extracted text locally. Supports resume and parallelism.

Prerequisites:
    pip install -e ".[google-ocr]"
    python scripts/ocr_pipeline/setup_google_docai.py

Usage:
    python scripts/ocr_pipeline/batch_ocr_docai.py --legislature 1
    python scripts/ocr_pipeline/batch_ocr_docai.py --all --resume --workers 8
    python scripts/ocr_pipeline/batch_ocr_docai.py --chambre senat --all --resume --workers 8
    python scripts/ocr_pipeline/batch_ocr_docai.py --pdf data/raw/archives_an/leg1/.../001.pdf
"""

import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.ocr.engines.google_docai import GoogleDocumentAIEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

RAW_DIRS = {
    "an": Path("data/raw/archives_an"),
    "senat": Path("data/raw/archives_senat/senat"),
}
OUTPUT_DIRS = {
    "an": Path("data/ocr_output/google-docai/an"),
    "senat": Path("data/ocr_output/google-docai/senat"),
}
DOCAI_MAX_PAGES = 15  # Document AI sync API limit

# Module-level refs set by run_batch()
RAW_DIR = RAW_DIRS["an"]
OUTPUT_DIR = OUTPUT_DIRS["an"]


class RateLimiter:
    """Ensure max N API calls per second across threads."""

    def __init__(self, max_per_second: float = 2.0):
        self._min_interval = 1.0 / max_per_second
        self._lock = threading.Lock()
        self._last_call = 0.0

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()


def discover_pdfs(legislature: int | None = None) -> list[Path]:
    """Find all PDFs to process."""
    base = RAW_DIR / f"leg{legislature}" if legislature else RAW_DIR
    if not base.exists():
        logger.error(f"Directory not found: {base}")
        return []
    pdfs = sorted(base.rglob("*.pdf"))
    logger.info(f"Found {len(pdfs)} PDFs in {base}")
    return pdfs


def output_path_for_pdf(pdf_path: Path) -> Path:
    """Compute output .txt path mirroring source structure."""
    rel = pdf_path.relative_to(RAW_DIR)
    return OUTPUT_DIR / rel.with_suffix(".txt")


def ocr_pdf(
    engine: GoogleDocumentAIEngine,
    pdf_path: Path,
    rate_limiter: RateLimiter | None = None,
) -> tuple[str, int]:
    """OCR a single PDF, splitting into chunks if > 15 pages. Returns (text, page_count)."""
    doc = fitz.open(pdf_path)
    page_count = len(doc)

    if page_count <= DOCAI_MAX_PAGES:
        doc.close()
        if rate_limiter:
            rate_limiter.acquire()
        return engine.ocr_pdf(pdf_path), page_count

    # Split into chunks of DOCAI_MAX_PAGES
    chunks_text = []
    for start in range(0, page_count, DOCAI_MAX_PAGES):
        end = min(start + DOCAI_MAX_PAGES, page_count) - 1
        chunk_pdf = fitz.open()
        chunk_pdf.insert_pdf(doc, from_page=start, to_page=end)
        pdf_bytes = chunk_pdf.tobytes()
        chunk_pdf.close()

        if rate_limiter:
            rate_limiter.acquire()
        text = engine._process_document(pdf_bytes, "application/pdf")
        chunks_text.append(text)

    doc.close()
    return "\n".join(chunks_text), page_count


def process_single_pdf(
    engine: GoogleDocumentAIEngine,
    pdf_path: Path,
    rate_limiter: RateLimiter | None = None,
) -> dict:
    """Process one PDF and save the result. Returns stats dict."""
    out_path = output_path_for_pdf(pdf_path)
    start = time.time()

    try:
        text, page_count = ocr_pdf(engine, pdf_path, rate_limiter)
        elapsed = time.time() - start

        # Atomic write: .tmp then rename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".txt.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.rename(out_path)

        return {
            "pdf": str(pdf_path),
            "pages": page_count,
            "chars": len(text),
            "time": round(elapsed, 1),
            "status": "ok",
        }
    except Exception as e:
        elapsed = time.time() - start
        logger.error(f"FAILED {pdf_path.name}: {e}")
        return {
            "pdf": str(pdf_path),
            "pages": 0,
            "chars": 0,
            "time": round(elapsed, 1),
            "status": f"error: {e}",
        }


def run_batch(
    legislature: int | None = None,
    pdf_path: str | None = None,
    all_legs: bool = False,
    resume: bool = False,
    workers: int = 4,
    chambre: str = "an",
):
    """Main batch processing loop."""
    global RAW_DIR, OUTPUT_DIR
    RAW_DIR = RAW_DIRS[chambre]
    OUTPUT_DIR = OUTPUT_DIRS[chambre]

    engine = GoogleDocumentAIEngine()

    # Discover PDFs
    if pdf_path:
        pdfs = [Path(pdf_path)]
    elif chambre == "senat" and all_legs:
        # Sénat: no legislature structure, just rglob all PDFs
        pdfs = discover_pdfs(legislature=None)
    elif all_legs:
        pdfs = []
        for leg in range(1, 12):
            pdfs.extend(discover_pdfs(legislature=leg))
    elif legislature:
        pdfs = discover_pdfs(legislature=legislature)
    else:
        logger.error("Specify --pdf, --legislature N, or --all")
        sys.exit(1)

    if not pdfs:
        logger.error("No PDFs found")
        sys.exit(1)

    # Resume: skip already processed
    if resume:
        pending = []
        for p in pdfs:
            out = output_path_for_pdf(p)
            if not out.exists() or out.stat().st_size == 0:
                pending.append(p)
        skipped = len(pdfs) - len(pending)
        if skipped > 0:
            logger.info(f"Resume: skipping {skipped} already processed PDFs")
        pdfs = pending

    if not pdfs:
        logger.info("All PDFs already processed!")
        return

    total = len(pdfs)
    logger.info(f"Processing {total} PDFs with {workers} workers")

    # Rate limiter: max 2 API calls/sec (120/min) to stay within Document AI quota
    rate_limiter = RateLimiter(max_per_second=2.0)

    # Process with thread pool
    all_stats = []
    log_file = OUTPUT_DIR / "batch_processing.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    completed = 0
    errors = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process_single_pdf, engine, pdf, rate_limiter): pdf
            for pdf in pdfs
        }

        for future in as_completed(futures):
            result = future.result()
            all_stats.append(result)
            completed += 1

            if result["status"] != "ok":
                errors += 1

            # Log each result
            with open(log_file, "a") as f:
                f.write(json.dumps(result, default=str) + "\n")

            # Progress
            elapsed = time.time() - start_time
            rate = completed / elapsed if elapsed > 0 else 0
            eta = (total - completed) / rate if rate > 0 else 0
            logger.info(
                f"[{completed}/{total}] {result['pdf'].split('/')[-1]} — "
                f"{result['pages']}p, {result['chars']}c, {result['time']}s "
                f"({'OK' if result['status'] == 'ok' else 'FAIL'}) "
                f"[ETA: {eta/60:.0f}min]"
            )

    # Summary
    total_time = time.time() - start_time
    total_pages = sum(s["pages"] for s in all_stats)
    total_chars = sum(s["chars"] for s in all_stats)

    print(f"\n{'=' * 60}")
    print(f"BATCH OCR COMPLETE")
    print(f"{'=' * 60}")
    print(f"  PDFs processed: {completed}")
    print(f"  Total pages:    {total_pages}")
    print(f"  Total chars:    {total_chars:,}")
    print(f"  Errors:         {errors}")
    print(f"  Wall time:      {total_time/60:.1f} min")
    print(f"  Avg per PDF:    {total_time/max(completed,1):.1f}s")
    print(f"  Avg per page:   {total_time/max(total_pages,1):.1f}s")
    print(f"  Output:         {OUTPUT_DIR}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Batch OCR archive PDFs with Google Document AI"
    )
    parser.add_argument("--pdf", type=str, help="Process a single PDF")
    parser.add_argument("--legislature", type=int, help="Process one legislature (1-11)")
    parser.add_argument("--all", action="store_true", help="Process all")
    parser.add_argument("--chambre", type=str, default="an", choices=["an", "senat"],
                        help="Chamber (default: an)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip already processed PDFs")
    parser.add_argument("--workers", type=int, default=8,
                        help="Parallel workers (default: 8)")
    args = parser.parse_args()

    run_batch(
        legislature=args.legislature,
        pdf_path=args.pdf,
        all_legs=args.all,
        resume=args.resume,
        workers=args.workers,
        chambre=args.chambre,
    )
