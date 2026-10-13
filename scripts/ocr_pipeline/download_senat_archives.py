#!/usr/bin/env python3
"""Download all Sénat archive PDFs (1958-1996) from senat.fr.

Uses ArchiveSenatCollector to scrape ~2000 PDFs from the 5th Republic archives.
Idempotent: skips already downloaded files.
Supports parallel downloads with multiple workers.

Usage:
    python scripts/ocr_pipeline/download_senat_archives.py
    python scripts/ocr_pipeline/download_senat_archives.py --start-year 1980
    python scripts/ocr_pipeline/download_senat_archives.py --workers 8
"""

import argparse
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.collectors.archive_senat import ArchiveSenatCollector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("data/raw/archives_senat")


def download_month_worker(year: int, month: int) -> dict:
    """Download all PDFs for a single month using a dedicated collector instance."""
    try:
        with ArchiveSenatCollector(output_dir=OUTPUT_DIR) as collector:
            paths = collector.download_month(year, month)
            return {
                "year": year,
                "month": month,
                "pdfs": len(paths),
                "status": "ok",
            }
    except Exception as e:
        logger.error(f"Failed month {year}/{month:02d}: {e}")
        return {
            "year": year,
            "month": month,
            "pdfs": 0,
            "status": f"error: {e}",
        }


def run(start_year: int = 1958, end_year: int = 1996, workers: int = 6):
    """Download all Sénat archive PDFs with parallel workers."""
    start_time = time.time()

    # Use a single collector to discover months
    with ArchiveSenatCollector(output_dir=OUTPUT_DIR) as collector:
        months = collector.list_available_months(
            start_year=start_year, end_year=end_year
        )
    logger.info(f"Found {len(months)} months to process ({start_year}-{end_year})")

    if not months:
        logger.error("No months found")
        return

    total_pdfs = 0
    errors = 0
    completed = 0
    total = len(months)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(download_month_worker, year, month): (year, month)
            for year, month in months
        }

        for future in as_completed(futures):
            result = future.result()
            completed += 1
            total_pdfs += result["pdfs"]

            if result["status"] != "ok":
                errors += 1

            if completed % 20 == 0 or completed == total:
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (total - completed) / rate if rate > 0 else 0
                logger.info(
                    f"[{completed}/{total}] "
                    f"{total_pdfs} PDFs, {errors} errors, "
                    f"{elapsed / 60:.0f}min elapsed "
                    f"[ETA: {eta / 60:.0f}min]"
                )

    # Count actual files
    all_pdfs = list(OUTPUT_DIR.rglob("*.pdf"))
    elapsed = time.time() - start_time

    print(f"\n{'=' * 60}")
    print(f"SÉNAT ARCHIVE DOWNLOAD COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Months processed: {len(months)}")
    print(f"  PDFs downloaded:  {total_pdfs}")
    print(f"  Total on disk:    {len(all_pdfs)}")
    print(f"  Errors:           {errors}")
    print(f"  Workers:          {workers}")
    print(f"  Time:             {elapsed / 60:.1f} min")
    print(f"  Output:           {OUTPUT_DIR}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download Sénat archive PDFs (1958-1996)"
    )
    parser.add_argument("--start-year", type=int, default=1958)
    parser.add_argument("--end-year", type=int, default=1996)
    parser.add_argument("--workers", type=int, default=6,
                        help="Parallel download workers (default: 6)")
    args = parser.parse_args()

    run(start_year=args.start_year, end_year=args.end_year, workers=args.workers)
