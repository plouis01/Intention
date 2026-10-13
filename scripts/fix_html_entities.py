"""Fix HTML entities in Sénat intervention data.

Sénat debates imported via XML/HTML contain unescaped HTML entities in
orateur, fonction, and contenu fields (e.g., pr&#233;sident → président).

This script decodes all HTML entities in the interventions table in-place.
Safe to run multiple times (idempotent - already-clean text is unchanged).
"""

import html
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.models import Intervention, get_session, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Fields to clean
FIELDS = ["orateur", "fonction", "contenu"]

# Patterns that indicate HTML entities
ENTITY_MARKERS = ["&#", "&amp;", "&lt;", "&gt;", "&quot;", "&eacute;", "&egrave;",
                   "&agrave;", "&ccedil;", "&ocirc;", "&ucirc;", "&acirc;"]


def has_entities(text: str) -> bool:
    """Check if text contains HTML entities."""
    if not text:
        return False
    return any(marker in text for marker in ENTITY_MARKERS)


def fix_html_entities(batch_size: int = 5000, dry_run: bool = False):
    """Clean HTML entities in interventions table."""
    init_db()
    session = get_session()

    # Count affected rows
    total = session.query(Intervention).count()
    logger.info(f"Total interventions: {total}")

    # Count rows with entities (sampling approach for speed)
    affected_count = 0
    for field in FIELDS:
        col = getattr(Intervention, field)
        count = session.query(Intervention).filter(col.like("%&#%")).count()
        if count > 0:
            logger.info(f"  {field}: {count} rows with &#... entities")
            affected_count = max(affected_count, count)

    if affected_count == 0:
        logger.info("No HTML entities found - database is clean.")
        session.close()
        return

    logger.info(f"Fixing up to {affected_count} rows (batch size: {batch_size})")

    if dry_run:
        # Show examples
        examples = (
            session.query(Intervention)
            .filter(Intervention.fonction.like("%&#%"))
            .limit(5)
            .all()
        )
        for ex in examples:
            logger.info(f"  Before: fonction='{ex.fonction}'")
            logger.info(f"  After:  fonction='{html.unescape(ex.fonction)}'")
        session.close()
        return

    # Process in batches
    offset = 0
    fixed_total = 0

    while offset < total:
        batch = (
            session.query(Intervention)
            .order_by(Intervention.id)
            .offset(offset)
            .limit(batch_size)
            .all()
        )

        if not batch:
            break

        fixed_in_batch = 0

        for intervention in batch:
            changed = False
            for field in FIELDS:
                value = getattr(intervention, field)
                if value and has_entities(value):
                    cleaned = html.unescape(value)
                    if cleaned != value:
                        setattr(intervention, field, cleaned)
                        changed = True

            if changed:
                fixed_in_batch += 1

        if fixed_in_batch > 0:
            session.commit()
            fixed_total += fixed_in_batch

        offset += batch_size
        logger.info(f"  Processed {min(offset, total)}/{total} - fixed {fixed_in_batch} in batch")

    session.close()
    logger.info(f"Done. Fixed {fixed_total} interventions total.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fix HTML entities in Sénat data")
    parser.add_argument("--dry-run", action="store_true", help="Show examples without modifying")
    parser.add_argument("--batch-size", type=int, default=5000, help="Batch size for updates")
    args = parser.parse_args()

    fix_html_entities(batch_size=args.batch_size, dry_run=args.dry_run)
