#!/usr/bin/env python3
"""Fix AN amendments with numero='?' by reading identification.numeroLong from raw JSON files.

After SQLite → PostgreSQL migration, 10,060 AN amendments have numero='?' because
the parser read amend.get("numeroLong") which is None at top level - the actual
numero lives in amend["identification"]["numeroLong"].

This script:
1. Queries all AN amendments where numero='?'
2. Looks up their raw JSON by reference_externe (uid)
3. Extracts identification.numeroLong
4. Updates the DB
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from src.db.models import get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

RAW_DIR = Path(__file__).parent.parent / "data" / "raw" / "assemblee"


def build_uid_to_file_index() -> dict[str, Path]:
    """Build an index of uid → JSON file path from raw AN amendment files."""
    index: dict[str, Path] = {}
    for leg_dir in sorted(RAW_DIR.glob("amendements_leg*")):
        json_dir = leg_dir / "json"
        if not json_dir.exists():
            continue
        for json_file in json_dir.rglob("*.json"):
            # The filename (without extension) is the uid
            uid = json_file.stem
            index[uid] = json_file
    logger.info(f"Indexed {len(index)} raw AN amendment files")
    return index


def extract_numero_from_json(file_path: Path) -> str | None:
    """Extract identification.numeroLong from a raw AN amendment JSON file."""
    try:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
        amend = data.get("amendement", data)
        identification = amend.get("identification", {}) or {}
        numero = identification.get("numeroLong")
        if numero:
            return str(numero)
    except Exception as e:
        logger.warning(f"Error reading {file_path}: {e}")
    return None


def main():
    session = get_session()
    try:
        # Step 1: Find all AN amendments with numero='?'
        result = session.execute(text("""
            SELECT a.id, a.reference_externe
            FROM amendements a
            JOIN dossiers_legislatifs d ON a.dossier_id = d.id
            WHERE a.numero = '?' AND d.chambre = 'AN'
        """))
        rows = result.fetchall()
        logger.info(f"Found {len(rows)} AN amendments with numero='?'")

        if not rows:
            logger.info("Nothing to fix.")
            return

        # Step 2: Build file index
        uid_index = build_uid_to_file_index()

        # Step 3: Look up and fix each amendment
        fixed = 0
        not_found = 0
        no_numero = 0

        for amend_id, ref_ext in rows:
            if not ref_ext or ref_ext not in uid_index:
                not_found += 1
                continue

            numero = extract_numero_from_json(uid_index[ref_ext])
            if not numero:
                no_numero += 1
                continue

            session.execute(
                text("UPDATE amendements SET numero = :numero WHERE id = :id"),
                {"numero": numero, "id": amend_id},
            )
            fixed += 1

        session.commit()

        logger.info(f"Results:")
        logger.info(f"  Fixed:          {fixed}")
        logger.info(f"  File not found: {not_found}")
        logger.info(f"  No numero in JSON: {no_numero}")
        logger.info(f"  Total processed: {len(rows)}")

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
