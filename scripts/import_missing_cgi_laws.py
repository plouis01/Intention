#!/usr/bin/env python3
"""Import pre-2002 laws from lois_cgi_1958_2026.json that are missing from the DB.

This adds the 455 laws referenced in the CGI modification list but not yet in the
database. For each law, it creates the Loi record and imports articles from
Légifrance JSON files (with LEGI dump fallback) when available.

After running this, re-run index_pre2002_modifications.py and
extract_cgi_from_debates.py to extract CGI article references from the new laws.

Usage:
    python scripts/import_missing_cgi_laws.py --dry-run
    python scripts/import_missing_cgi_laws.py
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.db.import_data import import_articles_from_legifrance
from src.db.models import Loi, get_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

REFERENCE_PATH = project_root / "data" / "lois_cgi_1958_2026.json"
LEGIFRANCE_DIR = project_root / "data" / "raw" / "legifrance"
CUTOFF_DATE = "2002-01-01"


def main():
    parser = argparse.ArgumentParser(description="Import missing pre-2002 CGI laws")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Load reference list
    with open(REFERENCE_PATH, encoding="utf-8") as f:
        ref_data = json.load(f)

    ref_pre2002 = [
        l for l in ref_data["lois"]
        if l["date_promulgation"] < CUTOFF_DATE
    ]
    logger.info(f"Reference: {len(ref_pre2002)} pre-2002 laws modifying CGI")

    # Get existing laws
    session = get_session()
    existing_numeros = {l.numero for l in session.query(Loi.numero).all()}
    missing = [l for l in ref_pre2002 if l["numero"] not in existing_numeros]
    logger.info(f"Already in DB: {len(ref_pre2002) - len(missing)}, missing: {len(missing)}")

    if not missing:
        logger.info("Nothing to import.")
        return 0

    # Check which have JSON files
    with_json = [l for l in missing if (LEGIFRANCE_DIR / f"{l['jorf']}.json").exists()]
    without_json = [l for l in missing if not (LEGIFRANCE_DIR / f"{l['jorf']}.json").exists()]
    logger.info(f"Missing laws with JSON: {len(with_json)}, without JSON: {len(without_json)}")

    if args.dry_run:
        print(f"\nDRY RUN - Would import {len(missing)} laws:")
        print(f"  With JSON (articles importable): {len(with_json)}")
        print(f"  Without JSON (law record only): {len(without_json)}")
        print(f"\n  Sample with JSON:")
        for l in with_json[:10]:
            print(f"    {l['numero']} ({l['type']}) - {l['date_promulgation']} - {l['titre'][:60]}")
        print(f"\n  Sample without JSON:")
        for l in without_json[:10]:
            print(f"    {l['numero']} ({l['type']}) - {l['date_promulgation']} - {l['titre'][:60]}")
        return 0

    # Import laws
    laws_created = 0
    articles_imported = 0

    for law_data in missing:
        try:
            date_prom = datetime.strptime(law_data["date_promulgation"], "%Y-%m-%d")
        except ValueError:
            logger.warning(f"Bad date for {law_data['numero']}: {law_data['date_promulgation']}")
            continue

        loi = Loi(
            numero=law_data["numero"],
            type=law_data.get("type", "LOI"),
            annee=law_data.get("annee_budgetaire", date_prom.year),
            date_promulgation=date_prom,
            titre=law_data.get("titre", ""),
            url_legifrance=law_data.get("url_legifrance", ""),
        )
        session.add(loi)
        session.flush()  # Get ID for article import
        laws_created += 1

        # Try to import articles
        articles = import_articles_from_legifrance(session, loi, LEGIFRANCE_DIR)
        if articles:
            articles_imported += len(articles)
            logger.info(f"  + {loi.numero}: {len(articles)} articles")

    session.commit()
    session.close()

    print(f"\nResults:")
    print(f"  Laws created: {laws_created}")
    print(f"  Articles imported: {articles_imported}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
