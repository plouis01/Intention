#!/usr/bin/env python3
"""Link orphaned debates to laws based on date proximity."""

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.models import Debat, DossierLegislatif, Loi, get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def create_missing_dossiers(session):
    """Create dossiers for laws that don't have any."""
    # Find laws without dossiers
    laws_without_dossiers = (
        session.query(Loi)
        .outerjoin(DossierLegislatif)
        .filter(DossierLegislatif.id.is_(None))
        .all()
    )

    created = 0
    for loi in laws_without_dossiers:
        # Create AN dossier for each law
        dossier = DossierLegislatif(
            loi_id=loi.id,
            chambre="AN",
            reference_externe=f"dossier_{loi.numero}",
            url_source=loi.url_legifrance,
        )
        session.add(dossier)
        created += 1
        logger.info(f"Created dossier for {loi.titre}")

    session.commit()
    logger.info(f"Created {created} missing dossiers")
    return created


def get_debate_window(loi: Loi, extended: bool = False) -> tuple[datetime, datetime]:
    """
    Get the expected debate window for a law.

    Finance laws are typically debated:
    - LF: Oct-Dec of year before (for next year's budget)
    - LFR: Various times, typically 2-4 months before promulgation
    - LFSS: Oct-Dec of year before

    Extended mode captures debates that may continue after promulgation or
    span longer periods (e.g., spring LFR debates).
    """
    promulgation = loi.date_promulgation

    if loi.type == "LF":
        # LF is debated in autumn before promulgation
        # e.g., LF 2015 (promulgated Dec 2014) debated Sep-Dec 2014
        if extended:
            start = promulgation - timedelta(days=150)  # ~5 months before
            end = promulgation + timedelta(days=60)  # Debates may continue into new year
        else:
            start = promulgation - timedelta(days=120)
            end = promulgation + timedelta(days=7)
    elif loi.type == "LFR":
        # LFR debates can happen across a wide window
        if extended:
            start = promulgation - timedelta(days=180)  # ~6 months before
            end = promulgation + timedelta(days=30)
        else:
            start = promulgation - timedelta(days=90)
            end = promulgation + timedelta(days=7)
    else:  # LFSS
        if extended:
            start = promulgation - timedelta(days=150)
            end = promulgation + timedelta(days=60)
        else:
            start = promulgation - timedelta(days=120)
            end = promulgation + timedelta(days=7)

    return start, end


def link_debates_to_dossiers(session):
    """Link orphaned debates to appropriate dossiers based on date."""
    # Get orphaned debates
    orphaned = session.query(Debat).filter(Debat.dossier_id.is_(None)).all()
    logger.info(f"Found {len(orphaned)} orphaned debates")

    if not orphaned:
        return 0

    # Build law -> dossier mapping with debate windows (single query with JOIN)
    law_dossier_rows = (
        session.query(Loi, DossierLegislatif)
        .join(DossierLegislatif, DossierLegislatif.loi_id == Loi.id)
        .all()
    )

    law_windows = []
    for loi, dossier in law_dossier_rows:
        start, end = get_debate_window(loi, extended=True)
        law_windows.append({
            "dossier_id": dossier.id,
            "promulgation": loi.date_promulgation,
            "start": start,
            "end": end,
        })

    logger.info(f"Built {len(law_windows)} law windows")

    # Match debates to laws by checking all windows (windows overlap, so we
    # cannot short-circuit with a sorted break)
    linked = 0

    for debate in orphaned:
        if not debate.date:
            continue

        best_dossier_id = None
        best_distance = timedelta(days=9999)

        for window in law_windows:
            if window["start"] <= debate.date <= window["end"]:
                distance = abs(window["promulgation"] - debate.date)
                if distance < best_distance:
                    best_distance = distance
                    best_dossier_id = window["dossier_id"]

        if best_dossier_id:
            debate.dossier_id = best_dossier_id
            linked += 1

    session.commit()
    logger.info(f"Linked {linked} debates to dossiers")
    return linked


def main():
    session = get_session()

    # Step 1: Create missing dossiers
    logger.info("Step 1: Creating missing dossiers...")
    created = create_missing_dossiers(session)

    # Step 2: Link orphaned debates
    logger.info("Step 2: Linking orphaned debates...")
    linked = link_debates_to_dossiers(session)

    # Stats
    orphaned_after = session.query(Debat).filter(Debat.dossier_id.is_(None)).count()

    logger.info("\n--- Summary ---")
    logger.info(f"Dossiers created: {created}")
    logger.info(f"Debates linked: {linked}")
    logger.info(f"Remaining orphaned: {orphaned_after}")

    session.close()


if __name__ == "__main__":
    main()
