"""Fix fonction by temporal lookup for known government members.

For named speakers whose fonction varies incorrectly, apply the correct
fonction based on their known government role during a specific period.

Usage:
    python scripts/fix_fonction_by_lookup.py              # Dry run
    python scripts/fix_fonction_by_lookup.py --apply      # Apply changes
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text as sa_text
from src.db.models import get_session

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Lookup table: (orateur pattern, date_start, date_end, correct_fonction)
# Sources: Wikipedia, Assemblée nationale, Sénat
# Only covers roles where the person appears in parliamentary debates
FONCTION_LOOKUP = [
    # === SAFE: Government members - always speak in official capacity ===
    # Gouvernement Macron II (2022-2024)
    ("Mme Agnès Pannier-Runacher", "2022-05-20", "2024-09-21", "ministre"),
    # Gouvernement Philippe / Castex (2018-2022)
    ("Mme Agnès Pannier-Runacher", "2018-10-16", "2020-07-06", "secrétaire d'État"),
    ("Mme Agnès Pannier-Runacher", "2020-07-06", "2022-05-20", "ministre déléguée"),
    # Gouvernement Ayrault / Valls (2012-2017)
    ("M. Jérôme Cahuzac", "2012-05-16", "2013-03-19", "ministre délégué"),
    ("M. Bernard Cazeneuve", "2012-05-16", "2014-04-02", "ministre délégué"),
    ("M. Bernard Cazeneuve", "2014-04-02", "2015-12-01", "ministre"),
    ("M. Bernard Cazeneuve", "2016-12-06", "2017-05-15", "ministre"),
    ("M. Christian Eckert", "2014-04-09", "2017-05-10", "secrétaire d'État"),
    # Gouvernement Chirac / Sarkozy
    ("M. Philippe Douste-Blazy", "2004-03-31", "2005-06-02", "ministre"),
    ("M. Xavier Bertrand", "2005-06-02", "2007-05-18", "ministre"),
    ("M. Xavier Bertrand", "2007-06-19", "2010-11-14", "ministre"),
    ("M. Éric Woerth", "2007-05-18", "2010-11-14", "ministre"),
    ("M. François Baroin", "2010-11-14", "2012-05-10", "ministre"),
    # === SAFE: Parliamentary roles in LF/LFR/LFSS debates ===
    # DB only contains finance law debates → these speakers always act in their
    # commission des finances role when speaking in these debates.
    # Sénat - rapporteurs généraux
    ("M. Philippe Marini", "1998-01-01", "2011-10-01", "rapporteur général"),
    ("M. Philippe Marini", "2011-10-01", "2014-10-01", "président de la commission des finances"),
    ("M. Albéric de Montgolfier", "2014-10-01", "2023-10-01", "rapporteur général"),
    # AN - rapporteurs généraux
    ("M. Gilles Carrez", "2002-06-01", "2012-06-20", "rapporteur général"),
    ("M. Gilles Carrez", "2012-06-20", "2017-06-20", "président de la commission des finances"),
    ("M. Christian Eckert", "2012-06-20", "2014-04-09", "rapporteur général"),
    ("M. Joël Giraud", "2017-06-28", "2020-07-06", "rapporteur général"),
    ("M. Laurent Saint-Martin", "2020-09-01", "2022-06-19", "rapporteur général"),
    ("M. Didier Migaud", "1997-06-01", "2010-02-23", "rapporteur général"),
    ("M. Henri Emmanuelli", "1997-06-01", "2000-02-01", "président de la commission des finances"),
]

# Fonctions considered incorrect for government members
WRONG_FONCTIONS = {'autre', 'president', 'rapporteur', 'senateur', 'depute',
                   'président de séance', None}


def dry_run(db):
    """Show what would be corrected."""
    logger.info("=" * 70)
    logger.info("DRY RUN - Fonction correction by temporal lookup")
    logger.info("=" * 70)

    total = 0
    for orateur, date_start, date_end, correct_fn in FONCTION_LOOKUP:
        # Count interventions that would be fixed
        count = db.execute(sa_text("""
            SELECT COUNT(*) FROM interventions i
            JOIN debats d ON i.debat_id = d.id
            WHERE i.orateur = :orateur
            AND d.date >= :d1 AND d.date <= :d2
            AND (i.fonction IN ('autre', 'president', 'rapporteur', 'senateur', 'depute', 'président de séance')
                 OR i.fonction IS NULL)
        """), {"orateur": orateur, "d1": date_start, "d2": date_end}).scalar()

        if count > 0:
            # Also count how many are already correct
            already_correct = db.execute(sa_text("""
                SELECT COUNT(*) FROM interventions i
                JOIN debats d ON i.debat_id = d.id
                WHERE i.orateur = :orateur
                AND d.date >= :d1 AND d.date <= :d2
                AND i.fonction = :fn
            """), {"orateur": orateur, "d1": date_start, "d2": date_end, "fn": correct_fn}).scalar()

            logger.info(f"\n  {orateur} ({date_start} → {date_end}) → {correct_fn}")
            logger.info(f"    Would fix: {count:,} | Already correct: {already_correct:,}")
            total += count

    logger.info(f"\n{'=' * 70}")
    logger.info(f"TOTAL would fix: {total:,}")


def apply_fixes(db):
    """Apply fonction corrections."""
    logger.info("=" * 70)
    logger.info("APPLYING - Fonction correction by temporal lookup")
    logger.info("=" * 70)

    total = 0
    for orateur, date_start, date_end, correct_fn in FONCTION_LOOKUP:
        result = db.execute(sa_text("""
            UPDATE interventions
            SET fonction = :fn
            WHERE id IN (
                SELECT i.id FROM interventions i
                JOIN debats d ON i.debat_id = d.id
                WHERE i.orateur = :orateur
                AND d.date >= :d1 AND d.date <= :d2
                AND (i.fonction IN ('autre', 'president', 'rapporteur', 'senateur', 'depute', 'président de séance')
                     OR i.fonction IS NULL)
            )
        """), {"fn": correct_fn, "orateur": orateur, "d1": date_start, "d2": date_end})

        count = result.rowcount
        if count > 0:
            logger.info(f"  {orateur} ({date_start}→{date_end}): {count:,} → {correct_fn}")
            total += count

    db.commit()
    logger.info(f"\n  TOTAL fixed: {total:,}")


def main():
    parser = argparse.ArgumentParser(description="Fix fonction by temporal lookup")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    with get_session() as db:
        if args.apply:
            apply_fixes(db)
        else:
            dry_run(db)


if __name__ == "__main__":
    main()
