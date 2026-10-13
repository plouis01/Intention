#!/usr/bin/env python3
"""Fetch articles via Légifrance API for laws still missing articles.

Fallback for laws not found in the DILA LEGI dump.
"""

import json
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.collectors.legifrance import LegifranceClient
from src.db.import_data import import_articles_from_legifrance
from src.db.models import Article, Loi, get_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
LEGIFRANCE_DIR = PROJECT_ROOT / "data" / "raw" / "legifrance"


def fetch_and_save_law(client: LegifranceClient, jorf_id: str, loi: Loi) -> bool:
    """Fetch a law from the API and save the JSON."""
    json_file = LEGIFRANCE_DIR / f"{jorf_id}.json"

    try:
        logger.info(f"Fetching {loi.numero} ({jorf_id}) from Légifrance API...")
        data = client.get_law_by_text_id(jorf_id)

        if not data:
            logger.warning(f"  Empty response for {jorf_id}")
            return False

        # Save / merge with existing JSON
        if json_file.exists():
            with open(json_file) as f:
                existing = json.load(f)
            # Preserve legi_articles if present
            if existing.get("legi_articles"):
                data["legi_articles"] = existing["legi_articles"]

        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logger.info(f"  Saved {json_file.name}")
        return True

    except Exception as e:
        logger.error(f"  Error fetching {jorf_id}: {e}")
        return False


def extract_jorf_id(loi: Loi) -> str | None:
    """Extract JORF text ID from law's legifrance URL."""
    if loi.url_legifrance:
        match = re.search(r"(JORFTEXT\d+)", loi.url_legifrance)
        if match:
            return match.group(1)
    return None


def main():
    session = get_session()

    # Find laws without articles
    laws_with_articles = {
        row[0] for row in session.query(Article.loi_id).distinct().all()
    }
    all_laws = session.query(Loi).order_by(Loi.annee).all()
    laws_missing = [l for l in all_laws if l.id not in laws_with_articles]

    logger.info(f"Laws missing articles: {len(laws_missing)}")
    if not laws_missing:
        logger.info("All laws have articles!")
        return

    for l in laws_missing:
        jorf_id = extract_jorf_id(l)
        logger.info(f"  {l.numero:20s} ({l.type} {l.annee}) - {jorf_id or 'NO JORF ID'}")

    # Initialize API client
    client = LegifranceClient()

    fetched = 0
    imported = 0

    for loi in laws_missing:
        jorf_id = extract_jorf_id(loi)
        if not jorf_id:
            logger.warning(f"  Skipping {loi.numero}: no JORF ID")
            continue

        # Fetch from API
        if fetch_and_save_law(client, jorf_id, loi):
            fetched += 1
            # Import articles
            articles = import_articles_from_legifrance(session, loi, LEGIFRANCE_DIR)
            if articles:
                logger.info(f"  Imported {len(articles)} articles for {loi.numero}")
                imported += len(articles)
            else:
                logger.warning(f"  No articles extracted for {loi.numero}")

        # Rate limiting
        time.sleep(1)

    session.commit()
    session.close()

    logger.info(f"\nFetched {fetched} laws, imported {imported} articles")


if __name__ == "__main__":
    main()
