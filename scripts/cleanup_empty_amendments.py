#!/usr/bin/env python3
"""Delete fully-empty Sénat amendments that are mis-linked AMELI stubs.

After SQLite → PostgreSQL migration audit, 3,075 Sénat amendments have ALL of:
  objet IS NULL, expose_motifs IS NULL, contenu IS NULL, sort IS NULL

These are empty shells from AMELI bulk import — wrong dossier matches with zero
useful data. They were never embedded or indexed.

This script:
1. Counts fully-empty amendments by chambre
2. Deletes Sénat ones (mis-linked AMELI stubs)
3. Logs AN ones (kept — they have reference_externe for traceability)
4. Reports counts
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from src.db.models import get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# All four content/outcome fields must be NULL for deletion
EMPTY_CONDITION = """
    a.objet IS NULL
    AND a.expose_motifs IS NULL
    AND a.contenu IS NULL
    AND a.sort IS NULL
"""


def main():
    session = get_session()
    try:
        # Step 1: Count fully-empty amendments by chambre
        result = session.execute(text(f"""
            SELECT d.chambre, COUNT(*)
            FROM amendements a
            JOIN dossiers_legislatifs d ON a.dossier_id = d.id
            WHERE {EMPTY_CONDITION}
            GROUP BY d.chambre
        """))
        counts = dict(result.fetchall())
        logger.info(f"Fully-empty amendments by chambre: {counts}")

        senat_count = counts.get("SENAT", 0)
        an_count = counts.get("AN", 0)

        if senat_count == 0:
            logger.info("No fully-empty Sénat amendments to delete.")
            return

        # Step 2: Verify none are in chunk_embeddings
        result = session.execute(text(f"""
            SELECT COUNT(*)
            FROM amendements a
            JOIN dossiers_legislatifs d ON a.dossier_id = d.id
            LEFT JOIN chunk_embeddings ce ON ce.amendement_id = a.id
            WHERE {EMPTY_CONDITION}
            AND d.chambre = 'SENAT'
            AND ce.id IS NOT NULL
        """))
        embedded_count = result.scalar()
        if embedded_count > 0:
            logger.error(f"ABORT: {embedded_count} empty Sénat amendments have embeddings!")
            return

        # Step 3: Delete fully-empty Sénat amendments
        result = session.execute(text(f"""
            DELETE FROM amendements
            WHERE id IN (
                SELECT a.id
                FROM amendements a
                JOIN dossiers_legislatifs d ON a.dossier_id = d.id
                WHERE {EMPTY_CONDITION}
                AND d.chambre = 'SENAT'
            )
        """))
        deleted = result.rowcount
        session.commit()

        logger.info(f"Results:")
        logger.info(f"  Deleted Sénat empty amendments: {deleted}")
        logger.info(f"  Kept AN empty amendments:       {an_count} (have reference_externe)")

        # Step 4: Verify remaining NULL objet count
        result = session.execute(text(
            "SELECT COUNT(*) FROM amendements WHERE objet IS NULL"
        ))
        remaining_null = result.scalar()
        logger.info(f"  Remaining NULL objet total:     {remaining_null}")

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
