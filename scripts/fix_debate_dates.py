"""Fix debate dates that were incorrectly set to datetime.now() during import.

When AN debate XML files had missing or malformed date fields, the parser
fell back to datetime.now() - causing debates from 2019 to get 2025/2026 dates.

This script extracts real dates from the reference_externe field (e.g., "CRAN20191115")
and updates the database.

Usage:
    python scripts/fix_debate_dates.py              # Dry-run (shows what would change)
    python scripts/fix_debate_dates.py --apply      # Actually update the database
"""

import argparse
import logging
import sys
from datetime import datetime

from src.db.models import Debat, get_session
from src.parsers.debates import _extract_date_from_reference

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def fix_debate_dates(apply: bool = False) -> None:
    """Find and fix debates with suspicious future dates."""
    cutoff = datetime(2024, 6, 1)

    with get_session() as session:
        # Find debates with dates after cutoff
        suspicious = (
            session.query(Debat)
            .filter(Debat.date > cutoff)
            .order_by(Debat.date)
            .all()
        )

        logger.info(f"Found {len(suspicious)} debates with date > {cutoff.date()}")

        fixed = 0
        unfixable = 0

        for debate in suspicious:
            ref = debate.reference_externe
            extracted = _extract_date_from_reference(ref)

            if extracted and extracted < cutoff:
                old_date = debate.date
                logger.info(
                    f"{'FIX' if apply else 'WOULD FIX'}: "
                    f"debate {debate.id} ({debate.chambre}) "
                    f"ref={ref} "
                    f"{old_date.date()} → {extracted.date()}"
                )
                if apply:
                    debate.date = extracted
                fixed += 1
            else:
                logger.warning(
                    f"UNFIXABLE: debate {debate.id} ({debate.chambre}) "
                    f"ref={ref} date={debate.date.date()} - "
                    f"could not extract date from reference"
                )
                unfixable += 1

        if apply and fixed > 0:
            session.commit()
            logger.info(f"\nCommitted {fixed} date fixes to database.")

        logger.info(f"\nSummary: {fixed} fixable, {unfixable} unfixable, {len(suspicious)} total")
        if not apply and fixed > 0:
            logger.info("Run with --apply to commit changes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix debate dates from datetime.now() fallback")
    parser.add_argument("--apply", action="store_true", help="Actually update the database (default: dry-run)")
    args = parser.parse_args()

    fix_debate_dates(apply=args.apply)
