"""Fix fake orateurs caused by parser splitting sentences mid-phrase.

The debate parser sometimes splits a sentence in the middle when a speaker
cites another person: "M. Alauzet a raison. En l'occurrence..."
becomes orateur="M. Alauzet a raison" + contenu="En l'occurrence..."

This script detects these fake orateurs and reconstructs the original
intervention by appending the fake text to the previous legitimate speaker.

Usage:
    python scripts/fix_fake_orateurs.py              # Dry run (default)
    python scripts/fix_fake_orateurs.py --apply      # Apply changes
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, and_, text
from src.db.models import get_session, Intervention, ChunkEmbedding

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

VERB_INDICATORS = [
    # Original verbs
    " a ", " est ", " était ", " avait ", " aurait ", " serait ", " devrait ",
    " mais ", " quand ", " lorsque ", " nous ", " vous ",
    " qui ", " dont ", " où ", " car ",
    # Extended patterns (questions écrites, citations)
    " attire ", " soulève ", " rappelle ", " depuis ", " souhaite ",
    " propose ", " demande ", " estime ", " le soin de ", " de défendre ",
    " en commission ", " ont été ", " sera ", " peut ", " doit ", " veut ",
]

TITLE_KEYWORDS = {
    "ministre", "secrétaire", "rapporteur", "président", "commission",
    "présidente", "délégué", "directeur", "commissaire",
}


def is_fake_orateur(orateur: str) -> bool:
    """Detect if an orateur string is actually a sentence fragment."""
    if len(orateur) <= 30:
        return False
    o_lower = orateur.lower()
    if not (o_lower.startswith("m. ") or o_lower.startswith("mme ")):
        return False

    # Find first verb/conjunction position
    verb_pos = len(orateur)
    has_verb = False
    for v in VERB_INDICATORS:
        idx = o_lower.find(v)
        if 0 < idx < verb_pos:
            has_verb = True
            verb_pos = idx

    if not has_verb:
        return False

    # Check if a title keyword appears before the verb (legitimate long title)
    for tk in TITLE_KEYWORDS:
        if 0 < o_lower.find(tk) < verb_pos:
            return False

    return True


# Patterns indicating questions écrites (normalize orateur, DON'T reconstruct)
QUESTION_PATTERNS = [
    " attire l'attention ", " souhaite attirer l'attention ",
    " souhaite appeler l'attention ", " appelle l'attention ",
    " interroge ",
    " rappelle à m",  # "rappelle à M. le ministre" but not "rappelle à juste titre"
    " rappelle à mme",
]
ADMIN_PATTERNS = [
    " ont été proclamés", " ont été enregistrés", " ont été inscrits",
    " a été proclamé", " a été enregistré", " a été nommé",
]


def is_question_ecrite(orateur: str) -> bool:
    """Detect if a fake orateur is actually a question écrite header."""
    o_lower = orateur.lower()
    return any(p in o_lower for p in QUESTION_PATTERNS)


def is_admin_result(orateur: str) -> bool:
    """Detect electoral/administrative results."""
    o_lower = orateur.lower()
    return any(p in o_lower for p in ADMIN_PATTERNS)


def extract_real_name(orateur: str) -> str:
    """Extract the real speaker name from a fake orateur.

    "M. Jacques Pelletier attire l'attention de M." → "M. Jacques Pelletier"
    "Mme Nicole Borvo attire l'attention..." → "Mme Nicole Borvo"
    """
    o_lower = orateur.lower()
    # Find first verb position
    verb_pos = len(orateur)
    for v in VERB_INDICATORS:
        idx = o_lower.find(v)
        if 0 < idx < verb_pos:
            verb_pos = idx
    # Take everything before the verb
    name = orateur[:verb_pos].strip()
    # Clean trailing punctuation
    name = name.rstrip('.,;:')
    return name


def dry_run(db):
    """Analyze fake orateurs: separate questions écrites from real fakes."""
    logger.info("=" * 70)
    logger.info("DRY RUN - Fake orateur detection (3 categories)")
    logger.info("=" * 70)

    # Get candidate interventions (long orateur) - avoid reading contenu
    candidates = db.query(
        Intervention.id, Intervention.debat_id, Intervention.orateur,
        Intervention.ordre,
    ).filter(
        func.length(Intervention.orateur) > 30,
    ).order_by(Intervention.debat_id, Intervention.ordre).all()

    all_fakes = [
        (r[0], r[1], r[2], r[3]) for r in candidates if is_fake_orateur(r[2])
    ]

    # Separate into 3 categories
    questions = []
    admin = []
    reconstruct = []
    for fid, did, fo, ford in all_fakes:
        if is_question_ecrite(fo):
            questions.append((fid, did, fo, ford))
        elif is_admin_result(fo):
            admin.append((fid, did, fo, ford))
        else:
            reconstruct.append((fid, did, fo, ford))

    logger.info(f"\nTotal fake orateurs: {len(all_fakes):,}")
    logger.info(f"  Questions écrites (NORMALIZE): {len(questions):,}")
    logger.info(f"  Admin/electoral (NORMALIZE): {len(admin):,}")
    logger.info(f"  Real faux (RECONSTRUCT): {len(reconstruct):,}")

    # --- Show NORMALIZE samples ---
    logger.info(f"\n--- QUESTIONS ÉCRITES: normalize orateur ---")
    for fid, did, fo, ford in questions[:10]:
        new_name = extract_real_name(fo)
        logger.info(f"  #{fid}: \"{fo[:65]}\" → \"{new_name}\"")

    logger.info(f"\n--- ADMIN: normalize orateur ---")
    for fid, did, fo, ford in admin:
        new_name = extract_real_name(fo)
        logger.info(f"  #{fid}: \"{fo[:65]}\" → \"{new_name}\"")

    # --- Show RECONSTRUCT analysis ---
    logger.info(f"\n--- REAL FAUX: reconstruct (attach to previous) ---")
    cascade_count = 0
    no_prev_count = 0
    success_count = 0
    sample_shown = 0

    for fid, did, fake_orateur, fake_ordre in reconstruct:
        prev = db.query(
            Intervention.id, Intervention.orateur, Intervention.fonction,
            func.length(Intervention.contenu), Intervention.ordre,
        ).filter(
            Intervention.debat_id == did,
            Intervention.ordre < fake_ordre,
        ).order_by(Intervention.ordre.desc()).first()

        if not prev:
            no_prev_count += 1
            continue

        prev_id, prev_orateur, prev_fn, prev_clen, prev_ordre = prev
        is_cascade = is_fake_orateur(prev_orateur)
        if is_cascade:
            cascade_count += 1
        success_count += 1

        if sample_shown < 15:
            cascade_tag = " [CASCADE]" if is_cascade else ""
            logger.info(f"\n  #{fid}{cascade_tag}:")
            logger.info(f"    FAKE: \"{fake_orateur[:80]}\"")
            logger.info(f"    → ATTACH TO #{prev_id}: \"{prev_orateur[:50]}\" ({prev_fn})")
            sample_shown += 1

    logger.info(f"\n{'=' * 70}")
    logger.info(f"SUMMARY")
    logger.info(f"{'=' * 70}")
    logger.info(f"  Questions écrites → NORMALIZE: {len(questions):,}")
    logger.info(f"  Admin → NORMALIZE: {len(admin):,}")
    logger.info(f"  Real faux → RECONSTRUCT: {len(reconstruct):,}")
    logger.info(f"    Reconstructable: {success_count:,}")
    logger.info(f"    Cascades: {cascade_count:,}")
    logger.info(f"    No previous: {no_prev_count:,}")


def apply_fixes(db):
    """Fix fake orateurs: normalize questions écrites, reconstruct real fakes."""
    logger.info("=" * 70)
    logger.info("APPLYING - Fake orateur fixes")
    logger.info("=" * 70)

    # Get candidates (long orateur) and filter in Python
    candidates = db.query(Intervention).filter(
        func.length(Intervention.orateur) > 30,
    ).order_by(Intervention.debat_id, Intervention.ordre).yield_per(5000).all()

    fake_interventions = [i for i in candidates if is_fake_orateur(i.orateur)]
    logger.info(f"Total fake interventions: {len(fake_interventions):,}")

    # --- Phase 1: NORMALIZE questions écrites + admin ---
    normalized = 0
    for fake in fake_interventions:
        if is_question_ecrite(fake.orateur) or is_admin_result(fake.orateur):
            new_name = extract_real_name(fake.orateur)
            # Prepend the removed part to contenu
            removed_part = fake.orateur[len(new_name):].strip()
            if removed_part:
                fake.contenu = removed_part + " " + (fake.contenu or "")
            fake.orateur = new_name
            normalized += 1

    db.flush()
    logger.info(f"  Phase 1 - Normalized: {normalized:,}")

    # --- Phase 2: RECONSTRUCT real fakes ---
    # Re-filter since some may have been normalized above
    remaining_fakes = [i for i in fake_interventions
                       if is_fake_orateur(i.orateur)]
    logger.info(f"  Phase 2 - Remaining fakes to reconstruct: {len(remaining_fakes):,}")

    attached = 0
    deleted_ids = []
    no_prev = 0

    for fake in remaining_fakes:
        prev = db.query(Intervention).filter(
            Intervention.debat_id == fake.debat_id,
            Intervention.ordre < fake.ordre,
            ~Intervention.id.in_(deleted_ids) if deleted_ids else True,
        ).order_by(Intervention.ordre.desc()).first()

        if not prev:
            no_prev += 1
            continue

        separator = " " if prev.contenu and prev.contenu[-1] in ".!?" else ". "
        reconstructed_text = f"{fake.orateur}{separator}{fake.contenu or ''}"
        prev.contenu = (prev.contenu or "") + " " + reconstructed_text

        deleted_ids.append(fake.id)
        attached += 1

    # Delete in batches
    if deleted_ids:
        batch_size = 500
        ce_deleted = 0
        for i in range(0, len(deleted_ids), batch_size):
            batch = deleted_ids[i:i + batch_size]
            ce_count = db.query(ChunkEmbedding).filter(
                ChunkEmbedding.intervention_id.in_(batch)
            ).delete(synchronize_session=False)
            ce_deleted += ce_count
        logger.info(f"  Chunk embeddings deleted: {ce_deleted:,}")

        for i in range(0, len(deleted_ids), batch_size):
            batch = deleted_ids[i:i + batch_size]
            db.query(Intervention).filter(
                Intervention.id.in_(batch)
            ).delete(synchronize_session=False)

    db.commit()

    logger.info(f"\n  RESULTS:")
    logger.info(f"  Normalized (questions/admin): {normalized:,}")
    logger.info(f"  Reconstructed (attached to prev): {attached:,}")
    logger.info(f"  Deleted: {len(deleted_ids):,}")
    logger.info(f"  No previous found: {no_prev:,}")


def main():
    parser = argparse.ArgumentParser(description="Fix fake orateurs (sentence fragments)")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    args = parser.parse_args()

    with get_session() as db:
        if args.apply:
            apply_fixes(db)
        else:
            dry_run(db)


if __name__ == "__main__":
    main()
