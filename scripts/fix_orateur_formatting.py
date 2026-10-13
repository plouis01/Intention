"""Fix orateur formatting issues: doubled names, trailing commas, truncated, non-standard.

Usage:
    python scripts/fix_orateur_formatting.py              # Dry run (default)
    python scripts/fix_orateur_formatting.py --apply      # Apply changes
"""

import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, text as sa_text
from src.db.models import get_session, Intervention, ChunkEmbedding

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


# ============================================================
# FIX A - Doubled names
# ============================================================

# Hard-coded fixes for edge cases in Cat A
A_EXACT_FIXES = {
    "M.leprésident. M. le président": "M. le président",
    "M. Dominiquede Legge. M. Dominique de Legge": "M. Dominique de Legge",
    "M. Arnaudde Belenet. M. Arnaud de Belenet": "M. Arnaud de Belenet",
}


def fix_a_doubled_names(db, apply=False):
    """Fix 'M. NomCollé. M. Nom Espacé' patterns."""
    logger.info("\n" + "=" * 70)
    logger.info("FIX A - DOUBLED NAMES")
    logger.info("=" * 70)

    rows = db.execute(sa_text("""
        SELECT orateur, COUNT(*) as cnt
        FROM interventions
        WHERE orateur ILIKE '%%. M. %%' AND length(orateur) > 20
        GROUP BY orateur ORDER BY cnt DESC
    """)).fetchall()

    total = sum(r[1] for r in rows)
    logger.info(f"Found: {len(rows)} distinct, {total} interventions")

    fixed = 0
    skipped = 0

    for orig, cnt in rows:
        # Check exact fixes first
        if orig in A_EXACT_FIXES:
            new_name = A_EXACT_FIXES[orig]
            if apply:
                db.execute(sa_text(
                    "UPDATE interventions SET orateur = :new WHERE orateur = :old"
                ), {"new": new_name, "old": orig})
            logger.info(f"  ✅ [{cnt:3d}x] \"{orig[:55]}\" → \"{new_name}\" (exact fix)")
            fixed += cnt
            continue

        # Split on ". M. " - take the right part
        if '. M. ' in orig:
            left = orig.split('. M. ', 1)[0]
            right = "M. " + orig.split('. M. ', 1)[1]
        elif '. Mme ' in orig:
            left = orig.split('. Mme ', 1)[0]
            right = "Mme " + orig.split('. Mme ', 1)[1]
        else:
            logger.info(f"  SKIP (no split pattern): \"{orig[:60]}\"")
            skipped += cnt
            continue

        # Check for OCR artifacts in right part:
        # - single-char words like "Gré gory" → "D antec"
        # - split words where right has more spaces than left (e.g. "Henri Caba nel" vs "HenriCabanel")
        right_words = right.split()
        left_clean = left.replace("M.", "").replace("Mme", "").strip()
        right_clean = right.replace("M.", "").replace("Mme", "").strip()
        has_single_char = any(len(w) == 1 and w not in ('M', 'A', 'É', 'Ô', 'de', 'La') for w in right_words[1:])
        has_extra_spaces = right_clean.count(' ') > left_clean.replace(' ', '').count(' ') + 2  # rough check
        # More reliable: check if removing spaces from right matches removing spaces from left
        left_nospace = re.sub(r'\s', '', left_clean).lower()
        right_nospace = re.sub(r'\s', '', right_clean).lower()
        has_ocr = has_single_char or (left_nospace == right_nospace and left_clean != right_clean)

        if has_ocr:
            # Use left part instead, fix the collapsed name
            # "M. JacquesMézard" → insert space before uppercase: "M. Jacques Mézard"
            name_part = left[3:] if left.startswith("M. ") else left[4:] if left.startswith("Mme ") else left
            # Handle "M.leprésident" → "leprésident"
            name_part = name_part.lstrip('.')
            prefix = "M. " if left.startswith("M") else "Mme "
            # Insert space before uppercase letters that follow lowercase
            fixed_name = re.sub(r'([a-zéèêëàâôîïùûü])([A-ZÉÈÊËÀÂÔÎÏÙÛÜ])', r'\1 \2', name_part)
            # Also fix "leprésident" → "le président"
            fixed_name = re.sub(r'^le([a-z])', r'le \1', fixed_name)
            new_name = prefix + fixed_name
            logger.info(f"  OCR FIX [{cnt}x]: \"{orig[:55]}\" → \"{new_name}\"")
        else:
            new_name = right.strip().rstrip(',')

        # Special case: "M. M. Xavier Bertrand" → already handled by split
        # Special case: too long (combined doublons) → just take the clean name
        if len(new_name) > 60:
            # Try to extract just the first clean name
            match = re.match(r'^(M\.|Mme)\s+[\w\s\-\']+', new_name)
            if match:
                new_name = match.group(0).strip()
            else:
                logger.info(f"  SKIP (too long after fix): \"{orig[:55]}\" → \"{new_name[:40]}\"")
                skipped += cnt
                continue

        if apply:
            db.execute(sa_text(
                "UPDATE interventions SET orateur = :new WHERE orateur = :old"
            ), {"new": new_name, "old": orig})
            fixed += cnt
        else:
            logger.info(f"  ✅ [{cnt:3d}x] \"{orig[:55]}\" → \"{new_name}\"")
            fixed += cnt

    if apply:
        db.commit()
    logger.info(f"\n  Fixed: {fixed:,}, Skipped: {skipped}")
    return fixed


# ============================================================
# FIX B - Trailing comma
# ============================================================

def fix_b_trailing_comma(db, apply=False):
    """Strip trailing commas from orateur names."""
    logger.info("\n" + "=" * 70)
    logger.info("FIX B - TRAILING COMMA")
    logger.info("=" * 70)

    count = db.execute(sa_text("""
        SELECT COUNT(*) FROM interventions
        WHERE length(orateur) > 10 AND orateur LIKE '%%,'
    """)).scalar()

    logger.info(f"Found: {count:,} interventions with trailing comma")

    if apply:
        db.execute(sa_text("""
            UPDATE interventions SET orateur = RTRIM(orateur, ',')
            WHERE length(orateur) > 10 AND orateur LIKE '%%,'
        """))
        db.commit()
        logger.info(f"  Fixed: {count:,}")
    else:
        logger.info(f"  Would fix: {count:,}")

    return count


# ============================================================
# FIX C - Truncated orateurs
# ============================================================

def fix_c_truncated(db, apply=False):
    """Fix truncated orateurs ('M', 'Mme', '', 'Bur', 'Pl')."""
    logger.info("\n" + "=" * 70)
    logger.info("FIX C - TRUNCATED ORATEURS")
    logger.info("=" * 70)

    truncated_values = ['M', 'Mme', '', 'Bur', 'Pl', 'M ']
    deleted = 0
    reconstructed = 0

    for trunc_val in truncated_values:
        rows = db.execute(sa_text("""
            SELECT i.id, i.fonction, i.debat_id, i.ordre, length(i.contenu) as clen
            FROM interventions i
            WHERE i.orateur = :val
            ORDER BY i.debat_id, i.ordre
        """), {"val": trunc_val}).fetchall()

        if not rows:
            continue

        logger.info(f"\n  orateur='{trunc_val}': {len(rows)} interventions")

        for iid, fn, did, ordre, clen in rows:
            # Short content = interjection → delete
            if clen and clen < 50:
                if apply:
                    db.execute(sa_text(
                        "DELETE FROM chunk_embeddings WHERE intervention_id = :id"
                    ), {"id": iid})
                    db.execute(sa_text(
                        "DELETE FROM interventions WHERE id = :id"
                    ), {"id": iid})
                deleted += 1
                continue

            # Longer content → reconstruct (attach to previous)
            prev = db.execute(sa_text("""
                SELECT id, orateur FROM interventions
                WHERE debat_id = :did AND ordre < :ordre
                ORDER BY ordre DESC LIMIT 1
            """), {"did": did, "ordre": ordre}).first()

            if prev and prev[1] and len(prev[1]) > 2:
                if apply:
                    # Read contenu individually (may fail on encoding)
                    try:
                        fake_contenu = db.execute(sa_text(
                            "SELECT contenu FROM interventions WHERE id = :id"
                        ), {"id": iid}).scalar()
                    except Exception:
                        db.rollback()
                        continue

                    try:
                        prev_contenu = db.execute(sa_text(
                            "SELECT contenu FROM interventions WHERE id = :id"
                        ), {"id": prev[0]}).scalar()
                    except Exception:
                        db.rollback()
                        continue

                    new_contenu = (prev_contenu or "") + " " + (trunc_val or "") + " " + (fake_contenu or "")
                    db.execute(sa_text(
                        "UPDATE interventions SET contenu = :c WHERE id = :id"
                    ), {"c": new_contenu, "id": prev[0]})
                    db.execute(sa_text(
                        "DELETE FROM chunk_embeddings WHERE intervention_id = :id"
                    ), {"id": iid})
                    db.execute(sa_text(
                        "DELETE FROM interventions WHERE id = :id"
                    ), {"id": iid})
                reconstructed += 1
            else:
                # No valid prev → delete
                if apply:
                    db.execute(sa_text(
                        "DELETE FROM chunk_embeddings WHERE intervention_id = :id"
                    ), {"id": iid})
                    db.execute(sa_text(
                        "DELETE FROM interventions WHERE id = :id"
                    ), {"id": iid})
                deleted += 1

    if apply:
        db.commit()
    logger.info(f"\n  Reconstructed: {reconstructed}, Deleted: {deleted}")
    return reconstructed + deleted


# ============================================================
# FIX D - Non-standard start
# ============================================================

# Exact fixes for known patterns
D_EXACT_FIXES = {
    "le président": "M. le président",
    "la présidente": "Mme la présidente",
    "président": "M. le président",
    "présidente": "Mme la présidente",
    "m le président": "M. le président",
    ". le président": "M. le président",
    "! M. le président": "M. le président",
    "1M. le président": "M. le président",
    "Monsieur le président": "M. le président",
    "Monsieur le Président": "M. le président",
    "Madame la présidente": "Mme la présidente",
    "Mmme la présidente": "Mme la présidente",
    "me Patricia Adam": "Mme Patricia Adam",
    "M .Patrick Roy": "M. Patrick Roy",
    ". François Rochebloine": "M. François Rochebloine",
    "Plusieur": "Plusieurs députés",
    "Josiane Corneloup": "Mme Josiane Corneloup",
    "Laure de La Raudière": "Mme Laure de La Raudière",
    "Lefèvre Mathieu M.": "M. Mathieu Lefèvre",
}

# Patterns to DELETE (date headers, not speakers)
D_DELETE_PATTERNS = [
    "Jeudi %", "Mardi %", "Mercredi %", "Lundi %", "Vendredi %", "Samedi %",
]

# Patterns that are LEGITIMATE (official headers, keep as-is)
D_KEEP_PATTERNS = [
    "Communication de %", "Allocution de %", "Déclaration de %",
    "Message de %", "Election de %", "Election du %",
    "De nombreux %", "Quelques %", "Des %",
    "Les députés %", "Les sénateurs %",
    "Emmanuelle Auriol",  # specific legitimate case
    "Pour toutes ces raisons%",  # fragment, keep as-is
    "À cette occasion%",  # fragment, keep as-is
]


def fix_d_non_standard(db, apply=False):
    """Fix non-standard orateur prefixes."""
    logger.info("\n" + "=" * 70)
    logger.info("FIX D - NON-STANDARD START")
    logger.info("=" * 70)

    rows = db.execute(sa_text("""
        SELECT orateur, fonction, COUNT(*) as cnt
        FROM interventions
        WHERE length(orateur) > 3
          AND orateur NOT ILIKE 'M.%%'
          AND orateur NOT ILIKE 'Mme%%'
          AND orateur NOT ILIKE 'Mlle%%'
          AND orateur NOT ILIKE 'Plusieurs%%'
          AND orateur NOT ILIKE 'Un %%'
          AND orateur NOT ILIKE 'Une %%'
          AND orateur NOT ILIKE 'Le %%'
          AND orateur NOT ILIKE 'La %%'
          AND orateur NOT ILIKE 'Groupe%%'
          AND orateur NOT ILIKE 'MM.%%'
          AND orateur NOT ILIKE 'Des %%'
          AND orateur NOT ILIKE 'Quelques%%'
          AND orateur NOT ILIKE 'De nombreux%%'
          AND length(orateur) < 60
        GROUP BY orateur, fonction ORDER BY cnt DESC
    """)).fetchall()

    total = sum(r[2] for r in rows)
    logger.info(f"Found: {len(rows)} distinct patterns, {total} interventions")

    fixed = 0
    kept = 0
    deleted = 0

    for orig, fn, cnt in rows:
        stripped = orig.strip()

        # Check exact fixes
        if stripped in D_EXACT_FIXES:
            new_name = D_EXACT_FIXES[stripped]
            if apply:
                db.execute(sa_text(
                    "UPDATE interventions SET orateur = :new WHERE orateur = :old"
                ), {"new": new_name, "old": orig})
            logger.info(f"  ✅ [{cnt}x] \"{stripped}\" → \"{new_name}\"")
            fixed += cnt
            continue

        # Check delete patterns
        is_delete = any(
            stripped.startswith(p.replace('%', '')) if '%' in p else stripped == p
            for p in D_DELETE_PATTERNS
        )
        if is_delete:
            if apply:
                db.execute(sa_text(
                    "DELETE FROM chunk_embeddings WHERE intervention_id IN (SELECT id FROM interventions WHERE orateur = :o)"
                ), {"o": orig})
                db.execute(sa_text(
                    "DELETE FROM interventions WHERE orateur = :o"
                ), {"o": orig})
            logger.info(f"  🗑️ [{cnt}x] DELETE \"{stripped}\"")
            deleted += cnt
            continue

        # Check keep patterns
        is_keep = any(
            stripped.startswith(p.replace('%', '')) if '%' in p else stripped == p
            for p in D_KEEP_PATTERNS
        )
        if is_keep:
            logger.info(f"  ⏭️ [{cnt}x] KEEP \"{stripped}\" (legitimate)")
            kept += cnt
            continue

        # Name without prefix: first letter uppercase + space = likely "Prénom Nom"
        if stripped[0].isupper() and ' ' in stripped and fn != 'president':
            new_name = f"M. {stripped}"
            if apply:
                db.execute(sa_text(
                    "UPDATE interventions SET orateur = :new WHERE orateur = :old"
                ), {"new": new_name, "old": orig})
            logger.info(f"  ✅ [{cnt}x] \"{stripped}\" → \"{new_name}\"")
            fixed += cnt
            continue

        # Fallback: investigate
        logger.info(f"  ❓ [{cnt}x] INVESTIGATE \"{stripped}\" (fn={fn})")

    if apply:
        db.commit()
    logger.info(f"\n  Fixed: {fixed}, Deleted: {deleted}, Kept: {kept}")
    return fixed + deleted


def main():
    parser = argparse.ArgumentParser(description="Fix orateur formatting issues")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    args = parser.parse_args()

    with get_session() as db:
        logger.info("=" * 70)
        logger.info(f"{'APPLYING' if args.apply else 'DRY RUN'} - Orateur formatting fixes")
        logger.info("=" * 70)

        a = fix_a_doubled_names(db, apply=args.apply)
        b = fix_b_trailing_comma(db, apply=args.apply)
        c = fix_c_truncated(db, apply=args.apply)
        d = fix_d_non_standard(db, apply=args.apply)

        logger.info(f"\n{'=' * 70}")
        logger.info("TOTAL")
        logger.info(f"{'=' * 70}")
        logger.info(f"  A (doubled names): {a:,}")
        logger.info(f"  B (trailing comma): {b:,}")
        logger.info(f"  C (truncated): {c:,}")
        logger.info(f"  D (non-standard): {d:,}")


if __name__ == "__main__":
    main()
