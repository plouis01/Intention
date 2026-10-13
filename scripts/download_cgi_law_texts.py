#!/usr/bin/env python3
"""Download full law texts for all CGI laws from Légifrance API.

This script downloads the complete text (with articles) for all laws
listed in data/lois_cgi_1958_2026.json that don't already have a JSON file.

Uses the existing LegifranceClient from src/collectors/legifrance.py.

Usage:
    python scripts/download_cgi_law_texts.py
    python scripts/download_cgi_law_texts.py --force  # Re-download existing files
    python scripts/download_cgi_law_texts.py --max 50  # Limit for testing
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.collectors.legifrance import LegifranceClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

CGI_LAWS_FILE = PROJECT_ROOT / "data" / "lois_cgi_1958_2026.json"
OUTPUT_DIR = PROJECT_ROOT / "data" / "raw" / "legifrance"


def main():
    parser = argparse.ArgumentParser(
        description="Download full law texts for CGI laws from Légifrance API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--force", action="store_true", help="Re-download existing files")
    parser.add_argument("--max", type=int, default=0, help="Max number of laws to download (0=all)")
    args = parser.parse_args()

    # Load reference file
    if not CGI_LAWS_FILE.exists():
        logger.error(f"Reference file not found: {CGI_LAWS_FILE}")
        sys.exit(1)

    with open(CGI_LAWS_FILE, encoding="utf-8") as f:
        data = json.load(f)

    laws = data.get("lois", [])
    logger.info(f"Loaded {len(laws)} laws from {CGI_LAWS_FILE}")

    # Filter: only laws with JORF ID
    laws_with_jorf = [law for law in laws if law.get("jorf")]
    logger.info(f"Found {len(laws_with_jorf)} laws with JORF ID")

    # Create output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Filter: skip existing files unless --force
    if not args.force:
        to_download = []
        for law in laws_with_jorf:
            jorf_id = law["jorf"]
            output_file = OUTPUT_DIR / f"{jorf_id}.json"
            if not output_file.exists():
                to_download.append(law)
        logger.info(f"Found {len(to_download)} laws to download (skipping {len(laws_with_jorf) - len(to_download)} existing)")
    else:
        to_download = laws_with_jorf
        logger.info(f"Force mode: will download all {len(to_download)} laws")

    # Limit for testing
    if args.max and args.max > 0:
        to_download = to_download[:args.max]
        logger.info(f"Limited to {len(to_download)} laws for testing")

    if not to_download:
        logger.info("Nothing to download. Exiting.")
        return

    # Download with LegifranceClient
    downloaded = 0
    failed = 0
    skipped = 0

    with LegifranceClient() as client:
        for i, law in enumerate(to_download, 1):
            jorf_id = law["jorf"]
            output_file = OUTPUT_DIR / f"{jorf_id}.json"

            logger.info(f"[{i}/{len(to_download)}] Downloading {law['numero']} - {jorf_id}")

            try:
                # Fetch full law text from API
                law_data = client.get_law_by_text_id(jorf_id)

                # Save to file
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(law_data, f, ensure_ascii=False, indent=2)

                downloaded += 1
                logger.info(f"  ✓ Saved to {output_file.name}")

                # Rate limiting: small delay between requests
                if i < len(to_download):
                    time.sleep(0.5)

            except Exception as e:
                failed += 1
                error_msg = str(e)

                # Check if it's a rate limit error
                if "429" in error_msg:
                    logger.warning(f"  ✗ Rate limited on {jorf_id}. Pausing 60s...")
                    time.sleep(60)
                    # Retry once
                    try:
                        law_data = client.get_law_by_text_id(jorf_id)
                        with open(output_file, "w", encoding="utf-8") as f:
                            json.dump(law_data, f, ensure_ascii=False, indent=2)
                        downloaded += 1
                        failed -= 1
                        logger.info(f"  ✓ Retry succeeded: {output_file.name}")
                    except Exception as retry_error:
                        logger.error(f"  ✗ Retry failed for {jorf_id}: {retry_error}")
                        # Create placeholder
                        _create_placeholder(output_file, law)
                        skipped += 1
                else:
                    logger.error(f"  ✗ Failed to download {jorf_id}: {error_msg}")
                    # Create placeholder for non-429 errors too
                    _create_placeholder(output_file, law)
                    skipped += 1

            # Progress report every 50
            if i % 50 == 0:
                logger.info(f"Progress: {downloaded} downloaded, {failed} failed, {skipped} placeholders")

    # Final summary
    logger.info("")
    logger.info("=" * 60)
    logger.info("DOWNLOAD SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total laws processed: {len(to_download)}")
    logger.info(f"Successfully downloaded: {downloaded}")
    logger.info(f"Failed (placeholders created): {skipped}")
    logger.info(f"Errors without placeholder: {failed - skipped}")
    logger.info("=" * 60)


def _create_placeholder(output_file: Path, law: dict):
    """Create a placeholder file for laws that can't be downloaded."""
    placeholder = {
        "text": {
            "id": law["jorf"],
            "title": law["titre"],
            "numero": law["numero"],
            "articles": [],
            "note": "Full text not available from API. Law metadata only.",
        },
        "source": "placeholder",
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(placeholder, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
