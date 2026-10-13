#!/usr/bin/env python3
"""Fetch missing law texts from DILA eliSearch / Journal Officiel API."""

import json
import logging
import sys
import time
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Missing JORF IDs
MISSING_LAWS = [
    ("JORFTEXT000025517888", "2012-354", "Loi de finances rectificative pour 2012 (1)"),
    ("JORFTEXT000026288994", "2012-958", "Loi de finances rectificative pour 2012 (2)"),
    ("JORFTEXT000026856788", "2012-1509", "Loi de finances pour 2013"),
    ("JORFTEXT000029988852", "2014-1654", "Loi de finances pour 2015"),
    ("JORFTEXT000029988957", "2014-1655", "Loi de finances rectificative pour 2014 (2)"),
    ("JORFTEXT000031732968", "2015-1786", "Loi de finances rectificative pour 2015"),
    ("JORFTEXT000042556490", "2020-1721", "Loi de finances rectificative pour 2020 (4)"),
    ("JORFTEXT000039682896", "2019-1446", "Loi de financement de la sécurité sociale pour 2020"),
    ("JORFTEXT000028388125", "2013-1203", "Loi de financement de la sécurité sociale pour 2014"),
]


def fetch_from_eli(numero: str, client: httpx.Client) -> dict | None:
    """Fetch law data using ELI (European Legislation Identifier)."""
    # ELI format: https://www.legifrance.gouv.fr/eli/loi/{year}/{month}/{day}/{numero}/jo/texte
    # But we'll use the JORF API directly

    # Try the JORF API with a different endpoint
    url = f"https://echanges.dila.gouv.fr/OPENDATA/JORF/Flux/"

    # This is a bulk download service, let's try another approach
    return None


def fetch_from_jo_api(jorf_id: str, client: httpx.Client) -> dict | None:
    """Try fetching from Journal Officiel OpenData."""
    # The DILA provides OpenData at echanges.dila.gouv.fr
    # But it requires registration for bulk access

    # Let's try a simpler approach: fetch the XML version
    xml_url = f"https://www.legifrance.gouv.fr/download/export/{jorf_id}.xml"

    try:
        response = client.get(xml_url)
        if response.status_code == 200:
            return {"xml": response.text, "source": "legifrance_xml"}
    except Exception as e:
        logger.debug(f"XML fetch failed: {e}")

    return None


def create_placeholder(jorf_id: str, numero: str, titre: str) -> dict:
    """Create a placeholder entry for laws we can't fetch."""
    return {
        "text": {
            "id": jorf_id,
            "title": titre,
            "numero": numero,
            "articles": [],
            "note": "Full text not available from API. Law metadata only.",
        },
        "source": "placeholder",
    }


def main():
    output_dir = Path("data/raw/legifrance")
    output_dir.mkdir(parents=True, exist_ok=True)

    existing = {p.stem for p in output_dir.glob("*.json")}
    to_fetch = [(jorf, num, titre) for jorf, num, titre in MISSING_LAWS if jorf not in existing]

    if not to_fetch:
        logger.info("All laws already have files!")
        return

    logger.info(f"Creating placeholder files for {len(to_fetch)} laws")
    logger.info("Note: Full text requires production API access or manual download")

    for jorf_id, numero, titre in to_fetch:
        output_file = output_dir / f"{jorf_id}.json"

        # Create placeholder so the import can proceed
        placeholder = create_placeholder(jorf_id, numero, titre)

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(placeholder, f, ensure_ascii=False, indent=2)

        logger.info(f"Created placeholder for {titre}")

    logger.info(f"\n--- Summary ---")
    logger.info(f"Total Légifrance files: {len(list(output_dir.glob('*.json')))}")
    logger.info("\nTo get full article texts for these laws:")
    logger.info("1. Subscribe to PISTE production API at https://piste.gouv.fr")
    logger.info("2. Or download from https://echanges.dila.gouv.fr/OPENDATA/JORF/")


if __name__ == "__main__":
    main()
