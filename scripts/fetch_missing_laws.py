#!/usr/bin/env python3
"""Fetch missing law texts from Légifrance API with rate limiting."""

import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.collectors.legifrance import LegifranceClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Missing JORF IDs to fetch
MISSING_LAWS = [
    ("JORFTEXT000025517888", "Loi de finances rectificative pour 2012 (1)"),
    ("JORFTEXT000026288994", "Loi de finances rectificative pour 2012 (2)"),
    ("JORFTEXT000026856853", "Loi de finances rectificative pour 2012 (3)"),
    ("JORFTEXT000026856788", "Loi de finances pour 2013"),
    ("JORFTEXT000029988852", "Loi de finances pour 2015"),
    ("JORFTEXT000029988957", "Loi de finances rectificative pour 2014 (2)"),
    ("JORFTEXT000031732865", "Loi de finances pour 2016"),
    ("JORFTEXT000031733082", "Loi de finances rectificative pour 2015"),
    ("JORFTEXT000033734169", "Loi de finances pour 2017"),
    ("JORFTEXT000033734341", "Loi de finances rectificative pour 2016"),
    ("JORFTEXT000036339197", "Loi de finances pour 2018"),
    ("JORFTEXT000042556490", "Loi de finances rectificative pour 2020 (4)"),
    ("JORFTEXT000039682896", "Loi de financement de la sécurité sociale pour 2020"),
    ("JORFTEXT000028388125", "Loi de financement de la sécurité sociale pour 2014"),
]


def is_placeholder(file_path: Path) -> bool:
    """Check if a file is a placeholder (no real content)."""
    try:
        with open(file_path) as f:
            data = json.load(f)
        return data.get("source") == "placeholder"
    except:
        return False


def main():
    output_dir = Path("data/raw/legifrance")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check which ones need fetching (missing or placeholder)
    to_fetch = []
    for jorf, titre in MISSING_LAWS:
        file_path = output_dir / f"{jorf}.json"
        if not file_path.exists() or is_placeholder(file_path):
            to_fetch.append((jorf, titre))

    if not to_fetch:
        logger.info("All laws already fetched with full content!")
        return

    logger.info(f"Need to fetch {len(to_fetch)} laws")

    with LegifranceClient() as client:
        for i, (jorf_id, titre) in enumerate(to_fetch):
            output_file = output_dir / f"{jorf_id}.json"

            try:
                logger.info(f"[{i+1}/{len(to_fetch)}] Fetching {titre} ({jorf_id})")
                law_data = client.get_law_by_text_id(jorf_id)

                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(law_data, f, ensure_ascii=False, indent=2)

                logger.info(f"  Saved {jorf_id}")

            except Exception as e:
                error_msg = str(e)
                if "429" in error_msg:
                    logger.warning(f"  Rate limited on {jorf_id}, waiting 60s...")
                    time.sleep(60)
                    # Retry once
                    try:
                        law_data = client.get_law_by_text_id(jorf_id)
                        with open(output_file, "w", encoding="utf-8") as f:
                            json.dump(law_data, f, ensure_ascii=False, indent=2)
                        logger.info(f"  Saved {jorf_id} after retry")
                    except Exception as e2:
                        logger.error(f"  Failed again: {e2}")
                elif "400" in error_msg:
                    logger.error(f"  Bad request for {jorf_id} - may not be in sandbox API")
                else:
                    logger.error(f"  Error fetching {jorf_id}: {e}")

            # Wait between requests to avoid rate limiting
            if i < len(to_fetch) - 1:
                logger.info("  Waiting 5s before next request...")
                time.sleep(5)

    # Final summary
    final_existing = {p.stem for p in output_dir.glob("*.json")}
    still_missing = [jorf for jorf, _ in MISSING_LAWS if jorf not in final_existing]

    logger.info(f"\n--- Summary ---")
    logger.info(f"Successfully fetched: {len(MISSING_LAWS) - len(still_missing)}")
    logger.info(f"Still missing: {len(still_missing)}")
    if still_missing:
        for jorf in still_missing:
            logger.info(f"  - {jorf}")


if __name__ == "__main__":
    main()
