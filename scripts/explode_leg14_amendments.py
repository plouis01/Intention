#!/usr/bin/env python3
"""Explode the single Amendements_XIV.json into individual files.

The leg14 archive from AN open data is a single large JSON with all amendments
nested inside. The import pipeline expects individual JSON files per amendment
with the same structure as legs 15-17: {"amendement": {..., "texteLegislatifRef": "..."}}.

This script reads the big JSON, extracts each amendment, injects
texteLegislatifRef from the parent texteleg level, and writes individual files
matching the directory layout used by legs 16-17.

Usage:
    python scripts/explode_leg14_amendments.py
"""

import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
INPUT_FILE = PROJECT_ROOT / "data" / "raw" / "assemblee" / "amendements_leg14" / "Amendements_XIV.json"
OUTPUT_DIR = PROJECT_ROOT / "data" / "raw" / "assemblee" / "amendements_leg14" / "json"


def main():
    if not INPUT_FILE.exists():
        logger.error(f"Input file not found: {INPUT_FILE}")
        sys.exit(1)

    logger.info(f"Loading {INPUT_FILE} ({INPUT_FILE.stat().st_size / 1e6:.0f} MB)...")
    with open(INPUT_FILE, encoding="utf-8") as f:
        data = json.load(f)

    textes = data["textesEtAmendements"]["texteleg"]
    logger.info(f"Found {len(textes)} textes législatifs")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    total = 0
    skipped = 0

    for texte in textes:
        text_ref = texte.get("refTexteLegislatif", "")
        amends_container = texte.get("amendements", {})

        # Handle both list and dict for amendement
        amend_list = amends_container.get("amendement", [])
        if isinstance(amend_list, dict):
            amend_list = [amend_list]

        if not amend_list:
            continue

        # Create subdirectory per text (mirrors leg16-17 layout: json/DOSSIER/TEXT/)
        text_dir = OUTPUT_DIR / text_ref
        text_dir.mkdir(parents=True, exist_ok=True)

        for amend in amend_list:
            uid = amend.get("uid")
            if not uid:
                skipped += 1
                continue

            # Inject texteLegislatifRef at top level for compatibility
            # (leg14 has it in identifiant.saisine.refTexteLegislatif)
            amend["texteLegislatifRef"] = text_ref

            # Write as {"amendement": {...}} like legs 15-17
            output_file = text_dir / f"{uid}.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump({"amendement": amend}, f, ensure_ascii=False)

            total += 1

        if total % 10000 == 0 and total > 0:
            logger.info(f"  Written {total} files...")

    logger.info(f"Done: {total} amendment files written to {OUTPUT_DIR}")
    if skipped:
        logger.warning(f"  Skipped {skipped} amendments without uid")


if __name__ == "__main__":
    main()
