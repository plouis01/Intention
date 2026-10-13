#!/usr/bin/env python3
"""Fix OCR-corrupted dates in archive debates using Claude CLI.

Many archive PDFs have OCR date errors: "1969" → "1069", "1985" → "1895".
This script finds those records and asks Claude to correct them by analyzing
the OCR text header + the reference_externe metadata.

Uses: claude --cli (installed separately)

Usage:
    python scripts/fix_archive_dates_claude.py --dry-run   # Preview changes
    python scripts/fix_archive_dates_claude.py              # Apply changes
"""

import argparse
import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from src.db.models import get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

OCR_OUTPUT_DIR = Path(__file__).parent.parent / "data" / "ocr_output" / "google-docai"


def find_ocr_file(reference_externe: str, chambre: str) -> Path | None:
    """Reconstruct OCR file path from reference_externe.

    Examples:
        archive_senat:s19681212_0001_0050 → data/ocr_output/google-docai/senat/**/**/s19681212_0001_0050.txt
        archive_an:1962-1963-extraordinaire1/047 → data/ocr_output/google-docai/an/leg*/1962-1963-extraordinaire1/047.txt
    """
    if chambre == "SENAT" and reference_externe.startswith("archive_senat:"):
        filename_stem = reference_externe.replace("archive_senat:", "")
        # Search for file with glob pattern (year/month structure unknown)
        for f in (OCR_OUTPUT_DIR / "senat").glob(f"**/{filename_stem}.txt"):
            return f
        logger.warning(f"OCR file not found for {reference_externe} (Sénat)")
        return None

    elif chambre == "AN" and reference_externe.startswith("archive_an:"):
        session_and_file = reference_externe.replace("archive_an:", "")
        # Format: "session_name/filename", with leg* directory between
        # Try all leg* directories
        for f in (OCR_OUTPUT_DIR / "an").glob(f"leg*/{session_and_file}.txt"):
            return f
        logger.warning(f"OCR file not found for {reference_externe} (AN)")
        return None

    return None


def ask_claude_for_date(ocr_header: str, reference_externe: str, bad_date: str, model: str = "opus") -> str | None:
    """Call Claude CLI to extract correct date from OCR text.

    Returns the date as YYYY-MM-DD string, or None if Claude couldn't determine it.
    """
    prompt = (
        f"Extrait OCR d'un Journal Officiel français. "
        f"La date actuellement en base de données est : {bad_date} (erronée - l'OCR a corrompu les chiffres). "
        f"Référence du débat : {reference_externe}. "
        f"\nDétermine la date correcte de ce débat en analysant l'en-tête du JO ci-dessous. "
        f"Réponds avec la date au format YYYY-MM-DD ou INCONNU.\n\n"
        f"{ocr_header}"
    )

    try:
        result = subprocess.run(
            ["claude", "--model", model, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            logger.warning(f"Claude CLI error for {reference_externe}: {result.stderr}")
            return None

        response = result.stdout.strip()

        # Extract date from response (Claude may include explanatory text)
        # Look for YYYY-MM-DD pattern anywhere in response, take the last occurrence
        matches = re.findall(r'\b(\d{4}-\d{2}-\d{2})\b', response)
        if matches:
            # Take the last date (Claude usually concludes with the correct date)
            return matches[-1]
        elif "INCONNU" in response.upper():
            logger.debug(f"Claude could not determine date for {reference_externe}")
            return None
        else:
            logger.warning(f"No date found in Claude response: {response[:100]}")
            return None

    except subprocess.TimeoutExpired:
        logger.warning(f"Claude CLI timeout for {reference_externe}")
        return None
    except FileNotFoundError:
        logger.error("Claude CLI not found. Install it with: pip install claude-code")
        return None
    except Exception as e:
        logger.warning(f"Claude CLI error for {reference_externe}: {e}")
        return None


def fix_dates(session, dry_run: bool = False, model: str = "opus") -> int:
    """Fix aberrant OCR dates by asking Claude to analyze OCR text."""
    logger.info("Querying debates with date < 1958-01-01...")
    logger.info(f"Using Claude model: {model}")

    rows = session.execute(text("""
        SELECT id, date, reference_externe, chambre
        FROM debats
        WHERE date < '1958-01-01'
        ORDER BY reference_externe
    """)).fetchall()

    logger.info(f"Found {len(rows)} debates with aberrant dates")

    fixed = 0
    skipped = 0
    unfixable = 0

    for debat_id, bad_date, ref_ext, chambre in rows:
        logger.info(f"\n[{fixed + skipped + unfixable + 1}/{len(rows)}] Processing {ref_ext} (date: {bad_date})")

        # Find OCR file
        ocr_file = find_ocr_file(ref_ext, chambre)
        if not ocr_file:
            unfixable += 1
            continue

        # Read OCR header
        try:
            with open(ocr_file, "r", encoding="utf-8") as f:
                ocr_header = f.read(5000)
        except Exception as e:
            logger.warning(f"Failed to read {ocr_file}: {e}")
            unfixable += 1
            continue

        # Ask Claude for correct date
        correct_date_str = ask_claude_for_date(ocr_header, ref_ext, str(bad_date), model=model)

        if not correct_date_str:
            skipped += 1
            continue

        # Validate date range (should be 1958-2002 for archives)
        try:
            corrected = datetime.strptime(correct_date_str, "%Y-%m-%d")
            if corrected.year < 1958 or corrected.year > 2002:
                logger.warning(
                    f"Claude date {correct_date_str} outside archive range (1958-2002), skipping"
                )
                skipped += 1
                continue
        except ValueError:
            logger.warning(f"Invalid date format from Claude: {correct_date_str}")
            skipped += 1
            continue

        logger.info(f"✓ Correcting {bad_date} → {correct_date_str}")

        if not dry_run:
            session.execute(
                text("UPDATE debats SET date = :new_date WHERE id = :id"),
                {"new_date": corrected, "id": debat_id},
            )

        fixed += 1

    if not dry_run and fixed > 0:
        session.commit()
        logger.info(f"\nCommitted {fixed} fixes to database")

    logger.info(f"\n=== SUMMARY ===")
    logger.info(f"Fixed: {fixed}")
    logger.info(f"Skipped (could not determine): {skipped}")
    logger.info(f"Unfixable (file not found): {unfixable}")

    return fixed


def verify(session) -> None:
    """Verify that fixes were applied."""
    count = session.execute(
        text("SELECT COUNT(*) FROM debats WHERE date < '1958-01-01'")
    ).scalar()

    status = "✓ OK" if count == 0 else "✗ WARNING"
    logger.info(f"{status}: Debates with date < 1958: {count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix OCR-corrupted dates in archive debates")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying DB")
    parser.add_argument("--model", default="opus", help="Claude model to use (default: opus)")
    args = parser.parse_args()

    session = get_session()
    try:
        fixed = fix_dates(session, dry_run=args.dry_run, model=args.model)
        verify(session)

        if args.dry_run and fixed > 0:
            logger.info(f"\nDry-run complete. Would fix {fixed} records. Run without --dry-run to apply.")
    finally:
        session.close()
