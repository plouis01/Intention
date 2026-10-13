#!/usr/bin/env python3
"""Patch OCR date errors in text files before re-parsing debates.

After correcting dates in the database via fix_archive_dates_claude.py,
this script updates the OCR text files themselves so the parser reads
the correct dates from the original source files.

Usage:
    python scripts/patch_ocr_dates.py --dry-run   # Preview changes
    python scripts/patch_ocr_dates.py              # Apply patches
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

OCR_OUTPUT_DIR = Path(__file__).parent.parent / "data" / "ocr_output" / "google-docai"

# Mapping: reference_externe → (bad_date, good_date)
# Extracted from the fix_archive_dates_claude.py run log
CORRECTIONS = {
    "archive_an:1958-1959-extraordinaire1/003": ("1950-01-17", "1959-01-16"),
    "archive_an:1958-1959-ordinaire1/002": ("1950-04-28", "1959-04-28"),
    "archive_an:1958-1959-ordinaire1/011": ("1950-05-27", "1959-05-27"),
    "archive_an:1958-1959-ordinaire1/027": ("1950-06-23", "1959-06-23"),
    "archive_an:1960-1961-droit/021": ("1951-06-21", "1961-06-20"),
    "archive_an:1961-1962-ordinaire2/018": ("1662-05-24", "1962-05-23"),
    "archive_an:1963-1964-ordinaire1/031": ("1903-10-30", "1963-10-30"),
    "archive_an:1963-1964-ordinaire1/049": ("1955-06-30", "1963-11-09"),
    "archive_an:1966-1967-ordinaire1/025": ("1906-10-21", "1966-10-21"),
    "archive_an:1966-1967-ordinaire1/056": ("1900-11-09", "1966-11-09"),
    "archive_an:1966-1967-ordinaire1/098": ("1956-12-19", "1966-12-19"),
    "archive_an:1967-1968-ordinaire2/008": ("1908-04-23", "1968-04-23"),
    "archive_an:1968-1969-droit1/010": ("1068-07-19", "1968-07-19"),
    "archive_an:1968-1969-ordinaire1/005": ("1068-10-04", "1968-10-04"),
    "archive_an:1968-1969-ordinaire1/047": ("1068-11-08", "1968-11-08"),
    "archive_an:1968-1969-ordinaire1/050": ("1908-11-09", "1968-11-09"),
    "archive_an:1969-1970-ordinaire1/025": ("1909-10-31", "1969-10-31"),
    "archive_an:1969-1970-ordinaire1/027": ("1060-11-03", "1969-11-03"),
    "archive_an:1969-1970-ordinaire1/032": ("1106-11-05", "1969-11-05"),
    "archive_an:1969-1970-ordinaire1/043": ("1909-11-14", "1969-11-13"),
    "archive_an:1969-1970-ordinaire1/061": ("1906-11-20", "1969-11-20"),
    "archive_an:1969-1970-ordinaire1/064": ("1060-11-21", "1969-11-21"),
    "archive_an:1979-1980-ordinaire1/044": ("1953-10-04", "1979-10-31"),
    "archive_an:1979-1980-ordinaire2/085": ("1080-06-23", "1980-06-23"),
    "archive_an:1980-1981-ordinaire1/050": ("1930-11-06", "1980-11-06"),
    "archive_an:1980-1981-ordinaire1/058": ("1930-11-13", "1980-11-13"),
    "archive_an:1981-1982-ordinaire1/087": ("1931-11-13", "1981-11-13"),
    "archive_an:1983-1984-ordinaire1/051": ("1903-11-03", "1983-11-03"),
    "archive_an:1983-1984-ordinaire1/131": ("1933-12-20", "1983-12-19"),
    "archive_an:1983-1984-ordinaire2/040": ("1904-05-03", "1984-05-03"),
    "archive_an:1983-1984-ordinaire2/079": ("1934-06-07", "1984-06-06"),
    "archive_an:1984-1985-ordinaire1/041": ("1904-10-29", "1984-10-29"),
    "archive_an:1984-1985-ordinaire1/052": ("1934-11-06", "1984-11-06"),
    "archive_an:1985-1986-extraordinaire1/009": ("1906-07-09", "1986-07-08"),
    "archive_an:1985-1986-ordinaire2/068": ("1906-06-11", "1986-06-10"),
    "archive_an:1985-1986-ordinaire2/083": ("1906-06-25", "1986-06-25"),
    "archive_an:1985-1986-ordinaire2/085": ("1906-06-26", "1986-06-25"),
    "archive_an:1985-1986-ordinaire2/086": ("1906-06-27", "1986-06-26"),
    "archive_an:1985-1986-ordinaire2/091": ("1906-07-01", "1986-06-30"),
    "archive_an:1985-1986-ordinaire2/092": ("1906-07-01", "1986-06-30"),
    "archive_an:1986-1987-ordinaire1/106": ("1906-12-09", "1986-12-08"),
    "archive_an:1986-1987-ordinaire2/108": ("1950-10-04", "1987-06-30"),
    "archive_an:1988-1989-ordinaire1/064": ("1588-11-17", "1988-11-16"),
    "archive_an:1989-1990-ordinaire1/068": ("1939-11-10", "1989-11-09"),
    "archive_an:1989-1990-ordinaire2/097": ("1950-06-28", "1990-06-27"),
    "archive_an:1990-1991-ordinaire1/116": ("1930-12-13", "1990-12-12"),
    "archive_an:1993-1994-ordinaire1/044": ("1400-10-05", "1993-10-29"),
    "archive_an:1993-1994-ordinaire2/071": ("1904-06-09", "1994-06-08"),
    "archive_an:1993-1994-ordinaire2/096": ("1934-06-24", "1994-06-23"),
    "archive_an:1994-1995-ordinaire1/086": ("1934-11-27", "1994-11-26"),
    "archive_senat:s19680723_0529_0560": ("1908-07-24", "1968-07-23"),
    "archive_senat:s19680724_0561_0608": ("1908-07-25", "1968-07-24"),
    "archive_senat:s19811027_2177_2222": ("1931-10-28", "1981-10-27"),
    "archive_senat:s19850516_0569_0581": ("1895-05-16", "1985-05-15"),
    "archive_senat:s19870414_0153_0182": ("1937-04-21", "1987-04-14"),
}


def find_ocr_file(reference_externe: str) -> Path | None:
    """Reconstruct OCR file path from reference_externe.

    Examples:
        archive_senat:s19681212_0001_0050 → data/ocr_output/google-docai/senat/**/**/s19681212_0001_0050.txt
        archive_an:1962-1963-extraordinaire1/047 → data/ocr_output/google-docai/an/leg*/1962-1963-extraordinaire1/047.txt
    """
    if reference_externe.startswith("archive_senat:"):
        filename_stem = reference_externe.replace("archive_senat:", "")
        for f in (OCR_OUTPUT_DIR / "senat").glob(f"**/{filename_stem}.txt"):
            return f
        logger.warning(f"OCR file not found for {reference_externe} (Sénat)")
        return None

    elif reference_externe.startswith("archive_an:"):
        session_and_file = reference_externe.replace("archive_an:", "")
        for f in (OCR_OUTPUT_DIR / "an").glob(f"leg*/{session_and_file}.txt"):
            return f
        logger.warning(f"OCR file not found for {reference_externe} (AN)")
        return None

    return None


def patch_ocr_file(ocr_file: Path, bad_date: str, good_date: str, dry_run: bool = False) -> bool:
    """Patch the OCR file by replacing the bad year with the good year in the header.

    Only modifies the first 200 chars (header) to avoid changing historical references in body.
    Returns True if patched, False if no changes needed.
    """
    try:
        with open(ocr_file, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.warning(f"Failed to read {ocr_file}: {e}")
        return False

    bad_year = bad_date[:4]
    good_year = good_date[:4]

    # Only patch the header (first 200 chars)
    header = content[:200]
    body = content[200:]

    if bad_year not in header:
        logger.debug(f"Year {bad_year} not found in header of {ocr_file.name}")
        return False

    # Replace year in header
    patched_header = header.replace(bad_year, good_year)

    if patched_header == header:
        logger.debug(f"No changes needed for {ocr_file.name}")
        return False

    patched_content = patched_header + body

    if not dry_run:
        try:
            with open(ocr_file, "w", encoding="utf-8") as f:
                f.write(patched_content)
            logger.info(f"✓ Patched {ocr_file.name}: {bad_year} → {good_year}")
        except Exception as e:
            logger.warning(f"Failed to write {ocr_file}: {e}")
            return False
    else:
        logger.info(f"[DRY-RUN] Would patch {ocr_file.name}: {bad_year} → {good_year}")

    return True


def patch_all(dry_run: bool = False) -> tuple[int, int, int]:
    """Patch all OCR files. Returns (patched, skipped, not_found)."""
    logger.info("Patching OCR date errors in text files...")
    logger.info(f"Total corrections to apply: {len(CORRECTIONS)}")

    patched = 0
    skipped = 0
    not_found = 0

    for i, (ref_ext, (bad_date, good_date)) in enumerate(CORRECTIONS.items(), 1):
        logger.info(f"\n[{i}/{len(CORRECTIONS)}] {ref_ext}")

        ocr_file = find_ocr_file(ref_ext)
        if not ocr_file:
            not_found += 1
            continue

        if patch_ocr_file(ocr_file, bad_date, good_date, dry_run=dry_run):
            patched += 1
        else:
            skipped += 1

    logger.info(f"\n=== SUMMARY ===")
    logger.info(f"Patched: {patched}")
    logger.info(f"Skipped (no changes): {skipped}")
    logger.info(f"Not found: {not_found}")

    return patched, skipped, not_found


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Patch OCR date errors in text files before re-parsing")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying files")
    args = parser.parse_args()

    patched, skipped, not_found = patch_all(dry_run=args.dry_run)

    if args.dry_run and patched > 0:
        logger.info(f"\nDry-run complete. Would patch {patched} files. Run without --dry-run to apply.")

    sys.exit(0 if not_found == 0 else 1)
