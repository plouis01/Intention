#!/usr/bin/env python3
"""
Import BOFiP (Bulletin Officiel des Finances Publiques) into sources_externes.

Fetches all BOFiP documents currently in force from the data.economie.gouv.fr API
and imports them as sources_externes entries with links to CGI articles.
"""

import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from src.db.models import init_db, get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

API_URL = "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/bofip-vigueur/records"
BATCH_SIZE = 100


def fetch_all_bofip() -> list[dict]:
    """Fetch all BOFiP documents in force."""
    all_records = []
    offset = 0

    # First get total count
    r = requests.get(API_URL, params={"limit": 0}, timeout=30)
    total = r.json().get("total_count", 0)
    logger.info(f"Total BOFiP documents: {total}")

    while offset < total:
        params = {
            "select": "titre,serie,division,identifiant_juridique,permalien,debut_de_validite,contenu",
            "limit": BATCH_SIZE,
            "offset": offset,
            "order_by": "identifiant_juridique",
        }
        r = requests.get(API_URL, params=params, timeout=60)
        if r.status_code != 200:
            logger.warning(f"HTTP {r.status_code} at offset {offset}, retrying...")
            time.sleep(5)
            continue

        data = r.json()
        records = data.get("results", [])
        all_records.extend(records)
        offset += BATCH_SIZE

        if offset % 1000 == 0:
            logger.info(f"  Fetched {offset}/{total}")

        time.sleep(0.3)

    logger.info(f"Fetched {len(all_records)} BOFiP documents")
    return all_records


def extract_cgi_articles(contenu: str) -> list[str]:
    """Extract CGI article references from BOFiP content."""
    if not contenu:
        return []

    # Pattern for CGI article references
    patterns = [
        r"article\s+(\d+(?:\s*[A-Z])?\s*(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies)?(?:\s*[A-Z])?)\s+du\s+(?:code\s+général\s+des\s+impôts|CGI)",
        r"(?:CGI|code général des impôts),?\s*(?:art\.?|article)\s+(\d+(?:[- ]\d+)?\s*[A-Z]?\s*(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies)?(?:\s*[A-Z])?)",
        r"(?:l')?article\s+(\d+(?:[- ]\d+)?\s*[A-Z]?\s*(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies)?(?:\s*[A-Z])?)\s+du\s+CGI",
    ]

    articles = set()
    for pattern in patterns:
        for match in re.finditer(pattern, contenu, re.IGNORECASE):
            art = match.group(1).strip()
            if art and len(art) < 30:
                articles.add(art)

    return sorted(articles)


def import_to_db(records: list[dict], dry_run: bool = False):
    """Import BOFiP records into sources_externes."""
    init_db()
    session = get_session()

    # Get existing BOFiP entries
    existing = set()
    result = session.execute(text(
        "SELECT url FROM sources_externes WHERE type = 'bofip'"
    ))
    for (url,) in result:
        existing.add(url)

    logger.info(f"Existing BOFiP in DB: {len(existing)}")

    new_count = 0
    for rec in records:
        url = rec.get("permalien", "")
        if not url or url in existing:
            continue

        contenu = rec.get("contenu", "")
        cgi_articles = extract_cgi_articles(contenu)

        date_str = rec.get("debut_de_validite", "")
        date_val = None
        if date_str:
            try:
                date_val = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        if dry_run:
            new_count += 1
            continue

        session.execute(text("""
            INSERT INTO sources_externes (type, titre, url, date, reference, contenu, articles_cgi_lies, date_collecte)
            VALUES ('bofip', :titre, :url, :date, :ref, :contenu, :articles_cgi, now())
        """), {
            "titre": rec.get("titre", "")[:500],
            "url": url,
            "date": date_val,
            "ref": rec.get("identifiant_juridique", ""),
            "contenu": contenu[:50000] if contenu else None,  # Limit size
            "articles_cgi": json.dumps(cgi_articles) if cgi_articles else None,
        })
        new_count += 1

        if new_count % 500 == 0:
            session.commit()
            logger.info(f"  Imported {new_count}")

    if not dry_run:
        session.commit()

    logger.info(f"{'Would import' if dry_run else 'Imported'} {new_count} new BOFiP documents")
    session.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    records = fetch_all_bofip()
    import_to_db(records, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
