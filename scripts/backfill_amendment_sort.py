#!/usr/bin/env python3
"""
Backfill missing 'sort' values for AN amendments from open data archives.

Downloads AN amendment archives (streaming), extracts UID → sort mapping,
and updates the database in batch.
"""

import io
import json
import logging
import sys
import zipfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from src.db.models import init_db, get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SORT_MAPPING = {
    "adopté": "adopte",
    "rejeté": "rejete",
    "retiré": "retire",
    "retiré avant séance": "retire",
    "tombé": "tombe",
    "non soutenu": "non_soutenu",
    "irrecevable": "irrecevable",
    "non renseigné": None,
}

AN_URLS = {
    16: "https://data.assemblee-nationale.fr/static/openData/repository/16/loi/amendements_div_legis/Amendements.json.zip",
    17: "https://data.assemblee-nationale.fr/static/openData/repository/17/loi/amendements_div_legis/Amendements.json.zip",
}


def extract_sort_from_amend(amend: dict) -> str | None:
    """Extract normalized sort from AN JSON amendment structure."""
    cycle = amend.get("cycleDeVie", {}) or {}
    sort_data = cycle.get("sort", amend.get("sort", {}))
    if isinstance(sort_data, dict):
        sort_raw = sort_data.get("libelle", sort_data.get("sortEnSeance", ""))
    else:
        sort_raw = str(sort_data) if sort_data else ""

    if not sort_raw:
        return None

    return SORT_MAPPING.get(sort_raw.lower().strip(), sort_raw.lower().strip())


def download_and_extract_sort(legislature: int) -> dict[str, str]:
    """Download AN archive and extract UID → sort mapping."""
    url = AN_URLS[legislature]
    logger.info(f"Downloading leg {legislature} amendments from {url}...")

    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    chunks = []
    downloaded = 0
    for chunk in resp.iter_content(chunk_size=1024 * 1024):
        chunks.append(chunk)
        downloaded += len(chunk)
        if total:
            pct = downloaded * 100 // total
            if pct % 10 == 0:
                logger.info(f"  Downloaded {downloaded // (1024*1024)} / {total // (1024*1024)} MB ({pct}%)")

    data = b"".join(chunks)
    logger.info(f"  Download complete: {len(data) // (1024*1024)} MB")

    uid_to_sort = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        json_files = [f for f in zf.namelist() if f.endswith(".json")]
        logger.info(f"  Parsing {len(json_files)} JSON files...")

        for i, name in enumerate(json_files):
            try:
                with zf.open(name) as f:
                    content = json.load(f)

                # Handle both single and batch formats
                if "amendement" in content:
                    amend = content["amendement"]
                    uid = amend.get("uid", "")
                    sort_val = extract_sort_from_amend(amend)
                    if uid and sort_val:
                        uid_to_sort[uid] = sort_val
                elif "amendements" in content:
                    for item in content["amendements"]:
                        amend = item.get("amendement", item)
                        uid = amend.get("uid", "")
                        sort_val = extract_sort_from_amend(amend)
                        if uid and sort_val:
                            uid_to_sort[uid] = sort_val
            except Exception:
                continue

            if (i + 1) % 10000 == 0:
                logger.info(f"  Parsed {i + 1}/{len(json_files)} files, found {len(uid_to_sort)} sort values")

    logger.info(f"  Extracted {len(uid_to_sort)} sort values for leg {legislature}")
    return uid_to_sort


def update_database(uid_to_sort: dict[str, str]):
    """Update amendments in database with sort values."""
    init_db()
    session = get_session()

    # Get amendments without sort that match our UIDs
    result = session.execute(text(
        "SELECT reference_externe FROM amendements WHERE (sort IS NULL OR sort = '') AND reference_externe IS NOT NULL"
    ))
    db_refs = {row[0] for row in result}

    # Match
    to_update = {}
    for uid, sort_val in uid_to_sort.items():
        if uid in db_refs:
            to_update[uid] = sort_val

    logger.info(f"Matching: {len(to_update)} amendments to update out of {len(db_refs)} without sort")

    # Batch update
    chunk_size = 500
    updated = 0
    refs = list(to_update.items())

    for i in range(0, len(refs), chunk_size):
        chunk = refs[i:i + chunk_size]
        for ref, sort_val in chunk:
            session.execute(
                text("UPDATE amendements SET sort = :sort WHERE reference_externe = :ref AND (sort IS NULL OR sort = '')"),
                {"sort": sort_val, "ref": ref},
            )
        session.commit()
        updated += len(chunk)
        if updated % 5000 == 0:
            logger.info(f"  Updated {updated}/{len(refs)}")

    logger.info(f"Updated {updated} amendments with sort values")
    session.close()


def main():
    all_sorts = {}
    for leg in sorted(AN_URLS.keys()):
        sorts = download_and_extract_sort(leg)
        all_sorts.update(sorts)

    logger.info(f"Total sort values extracted: {len(all_sorts)}")
    update_database(all_sorts)

    # Final stats
    init_db()
    session = get_session()
    result = session.execute(text("SELECT sort, count(*) FROM amendements GROUP BY sort ORDER BY count(*) DESC"))
    logger.info("Final sort distribution:")
    for row in result:
        logger.info(f"  {row[0] or 'NULL'}: {row[1]}")
    session.close()


if __name__ == "__main__":
    main()
