"""Fix incorrect 'fonction' metadata on Intervention records.

Usage:
    python scripts/fix_intervention_fonctions.py              # Dry run (default)
    python scripts/fix_intervention_fonctions.py --apply      # Apply changes
"""

import argparse
import logging
import re
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, update, and_, or_, case, text
from src.db.models import get_session, Intervention

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Correction rules, applied in order (most specific first)
# Each rule: (description, ilike_pattern, current_fonction, target_fonction)
#
# IMPORTANT: patterns use ILIKE which is case-insensitive substring match.
# To avoid false positives (e.g. "M. X a affirmé qu'un ministre..."),
# we use patterns that match how orateur names are actually formatted:
#   "M. le président", "Mme la présidente", "M. X, secrétaire d'État"
#   "M. le rapporteur", "M. X, rapporteur général"
ORATEUR_RULES = [
    # Passe A: "M. le président" / "Mme la présidente" (exact role, not mentions in text)
    ("orateur = 'M. le président' AND autre", "M. le président", "autre", "president"),
    ("orateur = 'Mme la présidente' AND autre", "Mme la présidente", "autre", "president"),
    ("orateur LIKE 'M. le président.%' AND autre", "M. le président.", "autre", "president"),
    ("orateur LIKE 'Mme la présidente.%' AND autre", "Mme la présidente.", "autre", "president"),
    # NULL fonction
    ("orateur = 'M. le président' AND NULL", "M. le président", None, "president"),
    ("orateur = 'Mme la présidente' AND NULL", "Mme la présidente", None, "president"),
    # Passe B: ", secrétaire d'État" pattern (comma before role in orateur string)
    ("orateur LIKE '%, secrétaire d'État%' AND autre", "%, secrétaire d'État%", "autre", "secrétaire d'État"),
    ("orateur LIKE '%, secrétaire d'Etat%' AND autre", "%, secrétaire d'Etat%", "autre", "secrétaire d'État"),
    ("orateur LIKE 'M. le secrétaire d'État%' AND autre", "M. le secrétaire d'État%", "autre", "secrétaire d'État"),
    ("orateur LIKE 'M. le secrétaire d'Etat%' AND autre", "M. le secrétaire d'Etat%", "autre", "secrétaire d'État"),
    ("orateur LIKE 'Mme la secrétaire d'État%' AND autre", "Mme la secrétaire d'État%", "autre", "secrétaire d'État"),
    # ", ministre délégué" pattern
    ("orateur LIKE '%, ministre délégué%' AND autre", "%, ministre délégué%", "autre", "ministre délégué"),
    # ", ministre" but NOT ", ministre délégué" (already handled above)
    ("orateur LIKE '%, ministre%' AND autre", "%, ministre%", "autre", "ministre"),
    ("orateur LIKE 'M. le ministre%' AND autre", "M. le ministre%", "autre", "ministre"),
    ("orateur LIKE 'Mme la ministre%' AND autre", "Mme la ministre%", "autre", "ministre"),
    # ", rapporteur" patterns
    ("orateur LIKE '%, rapporteur général%' AND autre", "%, rapporteur général%", "autre", "rapporteur général"),
    ("orateur LIKE 'M. le rapporteur général%' AND autre", "M. le rapporteur général%", "autre", "rapporteur général"),
    ("orateur LIKE '%, rapporteur spécial%' AND autre", "%, rapporteur spécial%", "autre", "rapporteur"),
    ("orateur LIKE '%, rapporteur pour avis%' AND autre", "%, rapporteur pour avis%", "autre", "rapporteur pour avis"),
    ("orateur LIKE '%, rapporteur%' AND autre", "%, rapporteur%", "autre", "rapporteur"),
    ("orateur LIKE 'M. le rapporteur%' AND autre", "M. le rapporteur%", "autre", "rapporteur"),
    ("orateur LIKE 'Mme la rapporteure%' AND autre", "Mme la rapporteure%", "autre", "rapporteur"),
    # Same patterns for NULL fonction
    ("orateur LIKE '%, secrétaire d'État%' AND NULL", "%, secrétaire d'État%", None, "secrétaire d'État"),
    ("orateur LIKE '%, ministre%' AND NULL", "%, ministre%", None, "ministre"),
    ("orateur LIKE 'M. le ministre%' AND NULL", "M. le ministre%", None, "ministre"),
    ("orateur LIKE '%, rapporteur%' AND NULL", "%, rapporteur%", None, "rapporteur"),
]

# Passe C: content-based detection patterns (first word of contenu)
CONTENT_ROLE_PATTERNS = [
    (re.compile(r'^rapporteur\s+général', re.IGNORECASE), "rapporteur général"),
    (re.compile(r'^rapporteur\s+pour\s+avis', re.IGNORECASE), "rapporteur pour avis"),
    (re.compile(r'^rapporteur', re.IGNORECASE), "rapporteur"),
    (re.compile(r'^secrétaire\s+d', re.IGNORECASE), "secrétaire d'État"),
    (re.compile(r'^ministre\s+délégué', re.IGNORECASE), "ministre délégué"),
    (re.compile(r'^ministre', re.IGNORECASE), "ministre"),
    (re.compile(r'^président\s+de\s+la\s+commission', re.IGNORECASE), "président de la commission"),
]


def dry_run(db):
    """Count how many rows each rule would affect."""
    logger.info("=" * 70)
    logger.info("DRY RUN - Counting affected rows per rule")
    logger.info("=" * 70)

    total_affected = 0

    # Current state
    total = db.query(func.count(Intervention.id)).scalar()
    autre = db.query(func.count(Intervention.id)).filter(Intervention.fonction == 'autre').scalar()
    null_f = db.query(func.count(Intervention.id)).filter(Intervention.fonction.is_(None)).scalar()
    logger.info(f"\nCurrent state: {total:,} total, {autre:,} 'autre', {null_f:,} NULL\n")

    # Passe A+B: orateur-based rules
    logger.info("--- Passe A+B: Orateur-based corrections ---\n")
    for desc, keyword, current_fn, target_fn in ORATEUR_RULES:
        fn_filter = Intervention.fonction.is_(None) if current_fn is None else Intervention.fonction == current_fn
        # Use exact match for simple names, ILIKE for patterns with %
        if '%' in keyword:
            name_filter = Intervention.orateur.ilike(keyword)
        else:
            name_filter = Intervention.orateur == keyword
        count = db.query(func.count(Intervention.id)).filter(fn_filter, name_filter).scalar()
        if count > 0:
            logger.info(f"  {desc}")
            logger.info(f"    → {target_fn}: {count:,} rows")
            total_affected += count

            # Show 3 sample orateurs
            samples = db.query(Intervention.orateur).filter(fn_filter, name_filter)\
                .distinct().limit(3).all()
            for (s,) in samples:
                logger.info(f"      ex: \"{s}\"")
            logger.info("")

    # Passe C: content-based (sample estimation)
    logger.info("--- Passe C: Content-based corrections (estimated from 5000 sample) ---\n")
    try:
        remaining_autre = db.query(Intervention.id, func.substr(Intervention.contenu, 1, 60))\
            .filter(Intervention.fonction == 'autre')\
            .limit(5000).all()
    except Exception as e:
        db.rollback()
        logger.warning(f"  Passe C skipped (encoding error in DB): {type(e).__name__}")
        remaining_autre = []

    passe_c_counts = {}
    for _id, content_start in remaining_autre:
        c = (content_start or "").strip()
        for pat, role in CONTENT_ROLE_PATTERNS:
            if pat.match(c):
                passe_c_counts[role] = passe_c_counts.get(role, 0) + 1
                break

    # Extrapolate from sample
    remaining_total = autre  # Approximate - some already covered by A+B
    sample_size = len(remaining_autre)
    for role, sample_count in sorted(passe_c_counts.items(), key=lambda x: -x[1]):
        estimated = int(sample_count / sample_size * remaining_total)
        logger.info(f"  contenu starts with '{role}': {sample_count}/{sample_size} sample → ~{estimated:,} estimated")
        total_affected += estimated

    logger.info(f"\n{'=' * 70}")
    logger.info(f"TOTAL estimated corrections: ~{total_affected:,} rows")
    logger.info(f"Remaining 'autre' after A+B+C: ~{autre - total_affected:,} (generic députés/sénateurs)")
    logger.info(f"{'=' * 70}")

    # Specific verification: Pannier-Runacher
    logger.info("\n--- Verification: Pannier-Runacher ---")
    pr_rows = db.query(Intervention.fonction, func.count(Intervention.id))\
        .filter(Intervention.orateur.ilike('%Pannier%'))\
        .group_by(Intervention.fonction).all()
    for fn, c in pr_rows:
        logger.info(f"  fonction={fn}: {c}")

    # Specific verification: Philippe Marini
    logger.info("\n--- Verification: Philippe Marini ---")
    pm_rows = db.query(Intervention.fonction, func.count(Intervention.id))\
        .filter(Intervention.orateur.ilike('%Marini%'))\
        .group_by(Intervention.fonction).all()
    for fn, c in pm_rows:
        logger.info(f"  fonction={fn}: {c}")


def apply_fixes(db):
    """Apply all correction rules."""
    logger.info("=" * 70)
    logger.info("APPLYING FIXES")
    logger.info("=" * 70)

    total_fixed = 0

    # Passe A+B: orateur-based rules (most specific first)
    for desc, keyword, current_fn, target_fn in ORATEUR_RULES:
        fn_filter = Intervention.fonction.is_(None) if current_fn is None else Intervention.fonction == current_fn
        name_filter = Intervention.orateur.ilike(keyword) if '%' in keyword else Intervention.orateur == keyword
        filters = and_(fn_filter, name_filter)

        count = db.query(func.count(Intervention.id)).filter(filters).scalar()
        if count > 0:
            db.execute(
                update(Intervention)
                .where(filters)
                .values(fonction=target_fn)
            )
            total_fixed += count
            logger.info(f"  Fixed {count:,}: {desc} → {target_fn}")

    # Commit passes A+B before attempting passe C
    db.commit()
    logger.info(f"\n  Passes A+B committed: {total_fixed:,} rows fixed")

    # Passe C: content-based - skipped (requires encoding fix in DB first)
    logger.info("  Passe C: skipped (encoding issues in DB prevent batch content scanning)")

    db.commit()
    logger.info(f"\n  TOTAL fixed: {total_fixed:,}")

    # Post-fix verification
    autre_after = db.query(func.count(Intervention.id)).filter(Intervention.fonction == 'autre').scalar()
    null_after = db.query(func.count(Intervention.id)).filter(Intervention.fonction.is_(None)).scalar()
    logger.info(f"  After: {autre_after:,} 'autre', {null_after:,} NULL")


def main():
    parser = argparse.ArgumentParser(description="Fix intervention fonction metadata")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    args = parser.parse_args()

    with get_session() as db:
        if args.apply:
            apply_fixes(db)
        else:
            dry_run(db)


if __name__ == "__main__":
    main()
