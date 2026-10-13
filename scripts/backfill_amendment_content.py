#!/usr/bin/env python3
"""
Backfill missing amendment content (contenu + expose_motifs) from AN open data archives.

Downloads AN amendment archives (streaming), extracts UID → content mapping,
and updates the database in batch.
"""

import html
import io
import json
import logging
import re
import sys
import zipfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from src.db.models import init_db, get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

AN_URLS = {
    16: "https://data.assemblee-nationale.fr/static/openData/repository/16/loi/amendements_div_legis/Amendements.json.zip",
    17: "https://data.assemblee-nationale.fr/static/openData/repository/17/loi/amendements_div_legis/Amendements.json.zip",
}


def strip_html(text_val: str | None) -> str | None:
    """Strip HTML tags and decode entities."""
    if not text_val:
        return None
    cleaned = re.sub(r"<[^>]+>", " ", text_val)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if cleaned else None


def extract_content_from_amend(amend: dict) -> dict:
    """Extract contenu + expose_motifs from AN JSON amendment."""
    corps = amend.get("corps", {}) or {}
    contenu_auteur = corps.get("contenuAuteur", {}) or {}

    dispositif = contenu_auteur.get("dispositif", "")
    expose = contenu_auteur.get("exposeSommaire", "")

    return {
        "contenu": strip_html(dispositif),
        "expose_motifs": strip_html(expose),
    }


def download_and_extract_content(legislature: int) -> dict[str, dict]:
    """Download AN archive and extract UID → {contenu, expose_motifs} mapping."""
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
        if total and downloaded % (50 * 1024 * 1024) < 1024 * 1024:
            logger.info(f"  {downloaded // (1024*1024)} / {total // (1024*1024)} MB")

    data = b"".join(chunks)
    logger.info(f"  Download complete: {len(data) // (1024*1024)} MB")

    uid_to_content = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        json_files = [f for f in zf.namelist() if f.endswith(".json")]
        logger.info(f"  Parsing {len(json_files)} JSON files...")

        for i, name in enumerate(json_files):
            try:
                with zf.open(name) as f:
                    content = json.load(f)

                if "amendement" in content:
                    amend = content["amendement"]
                    uid = amend.get("uid", "")
                    extracted = extract_content_from_amend(amend)
                    if uid and (extracted["contenu"] or extracted["expose_motifs"]):
                        uid_to_content[uid] = extracted
            except Exception:
                continue

            if (i + 1) % 20000 == 0:
                logger.info(f"  Parsed {i + 1}/{len(json_files)}, found {len(uid_to_content)} with content")

    logger.info(f"  Extracted content for {len(uid_to_content)} amendments from leg {legislature}")
    return uid_to_content


def update_database(uid_to_content: dict[str, dict], dry_run: bool = False):
    """Update amendments in database with content."""
    init_db()
    session = get_session()

    result = session.execute(text(
        "SELECT reference_externe FROM amendements WHERE (contenu IS NULL OR contenu = '') AND reference_externe IS NOT NULL"
    ))
    db_refs_without_content = {row[0] for row in result}

    to_update = {}
    for uid, content in uid_to_content.items():
        if uid in db_refs_without_content:
            to_update[uid] = content

    logger.info(f"Matching: {len(to_update)} amendments to update out of {len(db_refs_without_content)} without content")

    if dry_run:
        logger.info("DRY RUN - not updating")
        session.close()
        return

    updated = 0
    for ref, content in to_update.items():
        params = {"ref": ref}
        set_parts = []
        if content["contenu"]:
            set_parts.append("contenu = :contenu")
            params["contenu"] = content["contenu"]
        if content["expose_motifs"]:
            set_parts.append("expose_motifs = :expose")
            params["expose"] = content["expose_motifs"]

        if set_parts:
            session.execute(
                text(f"UPDATE amendements SET {', '.join(set_parts)} WHERE reference_externe = :ref"),
                params,
            )
            updated += 1

        if updated % 2000 == 0 and updated > 0:
            session.commit()
            logger.info(f"  Updated {updated}/{len(to_update)}")

    session.commit()
    logger.info(f"Updated {updated} amendments with content")
    session.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    all_content = {}
    for leg in sorted(AN_URLS.keys()):
        content = download_and_extract_content(leg)
        all_content.update(content)

    logger.info(f"Total content extracted: {len(all_content)}")
    update_database(all_content, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
