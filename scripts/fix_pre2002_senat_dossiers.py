#!/usr/bin/env python3
"""Create Sénat dossiers for pre-2002 laws and link orphaned Sénat debates.

Pre-2002 laws typically only have AN dossiers in the database.  This script:
  1. Creates a SENAT dossier for every law that has an AN dossier but no SENAT one.
  2. Links orphaned Sénat debates (dossier_id IS NULL) to the appropriate dossier
     using content-based matching (first 5 interventions) then date-based fallback.

Idempotent: skips dossiers that already exist (checked via reference_externe)
and debates that already have a dossier_id.
"""

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import and_, func, text as sa_text
from sqlalchemy.orm import Session

from src.db.models import (
    Debat,
    DossierLegislatif,
    Intervention,
    Loi,
    get_session,
)
from src.db.import_data import extract_law_reference_from_content


# ---------------------------------------------------------------------------
# Step 1 – Create missing Sénat dossiers
# ---------------------------------------------------------------------------

def find_laws_missing_senat_dossier(session: Session) -> list[Loi]:
    """Return laws that have at least one AN dossier but zero SENAT dossiers."""
    # Sub-query: loi_ids that already have a SENAT dossier
    senat_loi_ids = (
        session.query(DossierLegislatif.loi_id)
        .filter(DossierLegislatif.chambre == "SENAT")
        .subquery()
    )
    # Sub-query: loi_ids that have an AN dossier
    an_loi_ids = (
        session.query(DossierLegislatif.loi_id)
        .filter(DossierLegislatif.chambre == "AN")
        .distinct()
    )

    laws = (
        session.query(Loi)
        .filter(
            Loi.id.in_(an_loi_ids),
            ~Loi.id.in_(senat_loi_ids),
        )
        .order_by(Loi.annee, Loi.type)
        .all()
    )
    return laws


def create_senat_dossiers(session: Session) -> dict[int, DossierLegislatif]:
    """Create SENAT dossiers for laws that only have AN dossiers.

    Returns a mapping {loi_id: new_dossier} for the records just created.
    """
    laws = find_laws_missing_senat_dossier(session)
    print(f"[Step 1] Laws with AN dossier but no SENAT dossier: {len(laws)}")

    created: dict[int, DossierLegislatif] = {}
    for loi in laws:
        ref = f"auto_created_senat:{loi.numero}"
        # Guard: skip if already exists (idempotent)
        existing = (
            session.query(DossierLegislatif)
            .filter_by(reference_externe=ref)
            .first()
        )
        if existing:
            created[loi.id] = existing
            continue

        dossier = DossierLegislatif(
            loi_id=loi.id,
            chambre="SENAT",
            reference_externe=ref,
            url_source=None,
            etapes=None,
        )
        session.add(dossier)
        created[loi.id] = dossier
        print(f"  + Created SENAT dossier for {loi.type} {loi.annee} ({loi.numero})")

    session.flush()  # assign IDs
    print(f"[Step 1] Created {len(created)} SENAT dossier(s)\n")
    return created


# ---------------------------------------------------------------------------
# Step 2 – Link orphaned Sénat debates
# ---------------------------------------------------------------------------

def get_orphaned_senat_debates(session: Session) -> list[Debat]:
    """Return SENAT debates with dossier_id IS NULL."""
    return (
        session.query(Debat)
        .filter(
            Debat.chambre == "SENAT",
            Debat.dossier_id.is_(None),
        )
        .order_by(Debat.date)
        .all()
    )


def get_debate_content_sample(session: Session, debat_id: int) -> str:
    """Concatenate the first 5 interventions of a debate for content matching."""
    interventions = (
        session.query(Intervention.contenu)
        .filter_by(debat_id=debat_id)
        .order_by(Intervention.ordre)
        .limit(5)
        .all()
    )
    return "\n".join(row.contenu or "" for row in interventions)


def build_loi_lookup(session: Session) -> dict[tuple[str, int], list[Loi]]:
    """Build {(type, annee): [Loi, ...]} for all finance laws."""
    from collections import defaultdict

    lois = session.query(Loi).all()
    lookup: dict[tuple[str, int], list[Loi]] = defaultdict(list)
    for loi in lois:
        lookup[(loi.type, loi.annee)].append(loi)
    return lookup


def build_senat_dossier_lookup(session: Session) -> dict[int, DossierLegislatif]:
    """Build {loi_id: DossierLegislatif} for all SENAT dossiers."""
    dossiers = (
        session.query(DossierLegislatif)
        .filter_by(chambre="SENAT")
        .all()
    )
    return {d.loi_id: d for d in dossiers}


def match_debate_by_content(
    session: Session,
    debate: Debat,
    loi_lookup: dict[tuple[str, int], list[Loi]],
    senat_dossiers: dict[int, DossierLegislatif],
) -> DossierLegislatif | None:
    """Try to match a debate to a dossier via its intervention content."""
    # First try contenu_integral on the debate itself
    content = debate.contenu_integral or ""
    # If no full text, sample the first interventions
    if len(content) < 100:
        content = get_debate_content_sample(session, debate.id)
    if not content:
        return None

    ref = extract_law_reference_from_content(content)
    if ref is None:
        return None

    law_type, year = ref
    candidates = loi_lookup.get((law_type, year), [])
    for loi in candidates:
        dossier = senat_dossiers.get(loi.id)
        if dossier:
            return dossier

    return None


def match_debate_by_date(
    debate: Debat,
    loi_lookup: dict[tuple[str, int], list[Loi]],
    senat_dossiers: dict[int, DossierLegislatif],
) -> DossierLegislatif | None:
    """Fallback: match a Sénat debate to a law based on debate date heuristics.

    Heuristics:
      - Oct-Dec debates → LF / LFSS for the *next* year
      - Any debate within 30-90 days before a LFR's promulgation → that LFR
      - Jan-Sep debates → LF for the current year (ongoing discussion)
    """
    if debate.date is None:
        return None

    debate_date: datetime = debate.date
    debate_year = debate_date.year
    debate_month = debate_date.month

    # --- LFR window check (any month) ---
    # A LFR is usually debated in the weeks before promulgation.
    for (law_type, year), lois in loi_lookup.items():
        if law_type != "LFR":
            continue
        for loi in lois:
            if loi.date_promulgation is None:
                continue
            promulgation = loi.date_promulgation
            if isinstance(promulgation, str):
                try:
                    promulgation = datetime.fromisoformat(promulgation)
                except ValueError:
                    continue
            days_before = (promulgation - debate_date).days
            if 0 <= days_before <= 90:
                dossier = senat_dossiers.get(loi.id)
                if dossier:
                    return dossier

    # --- Seasonal heuristic ---
    if debate_month >= 10:
        # Oct-Dec → LF or LFSS for next year
        target_year = debate_year + 1
        for law_type in ("LF", "LFSS"):
            for loi in loi_lookup.get((law_type, target_year), []):
                dossier = senat_dossiers.get(loi.id)
                if dossier:
                    return dossier
    else:
        # Jan-Sep → current year LF (rare for initial, but possible for LFR/LFSS)
        target_year = debate_year
        for law_type in ("LF", "LFR", "LFSS"):
            for loi in loi_lookup.get((law_type, target_year), []):
                dossier = senat_dossiers.get(loi.id)
                if dossier:
                    return dossier

    return None


def ensure_senat_dossier(
    session: Session,
    loi: Loi,
    senat_dossiers: dict[int, DossierLegislatif],
) -> DossierLegislatif:
    """Return the existing SENAT dossier for a law, or create one on the fly."""
    if loi.id in senat_dossiers:
        return senat_dossiers[loi.id]

    ref = f"auto_created_senat:{loi.numero}"
    existing = (
        session.query(DossierLegislatif)
        .filter_by(reference_externe=ref)
        .first()
    )
    if existing:
        senat_dossiers[loi.id] = existing
        return existing

    dossier = DossierLegislatif(
        loi_id=loi.id,
        chambre="SENAT",
        reference_externe=ref,
    )
    session.add(dossier)
    session.flush()
    senat_dossiers[loi.id] = dossier
    print(f"  + Created on-the-fly SENAT dossier for {loi.type} {loi.annee} ({loi.numero})")
    return dossier


def link_orphaned_debates(session: Session) -> int:
    """Link orphaned Sénat debates to their matching SENAT dossier.

    Returns the number of debates linked.
    """
    orphans = get_orphaned_senat_debates(session)
    print(f"[Step 2] Orphaned SENAT debates (dossier_id IS NULL): {len(orphans)}")

    if not orphans:
        print("[Step 2] Nothing to link.\n")
        return 0

    loi_lookup = build_loi_lookup(session)
    senat_dossiers = build_senat_dossier_lookup(session)

    linked = 0
    unmatched = 0

    for debate in orphans:
        # (a) Content-based matching
        dossier = match_debate_by_content(session, debate, loi_lookup, senat_dossiers)

        # (b) Date-based fallback
        if dossier is None:
            dossier = match_debate_by_date(debate, loi_lookup, senat_dossiers)

        if dossier is not None:
            debate.dossier_id = dossier.id
            linked += 1
            date_str = debate.date.strftime("%Y-%m-%d") if debate.date else "?"
            # Retrieve the loi for logging
            loi = session.query(Loi).filter_by(id=dossier.loi_id).first()
            loi_label = f"{loi.type} {loi.annee}" if loi else f"loi_id={dossier.loi_id}"
            print(f"  -> Linked debate {debate.id} ({date_str}) to {loi_label}")
        else:
            unmatched += 1

    session.flush()
    print(f"[Step 2] Linked: {linked}, unmatched: {unmatched}\n")
    return linked


# ---------------------------------------------------------------------------
# Step 3 – Fix chamber mismatch (SENAT debates linked to AN dossiers)
# ---------------------------------------------------------------------------

def fix_chamber_mismatch(session: Session) -> int:
    """Relink SENAT debates from AN dossiers to their SENAT counterpart.

    When SENAT debates were linked before SENAT dossiers existed, they got
    assigned to AN dossiers. This moves them to the correct SENAT dossier.
    """
    result = session.execute(sa_text("""
        UPDATE debats d
        SET dossier_id = dl_senat.id
        FROM dossiers_legislatifs dl_an, dossiers_legislatifs dl_senat
        WHERE d.dossier_id = dl_an.id
        AND dl_an.chambre = 'AN'
        AND d.chambre = 'SENAT'
        AND dl_senat.loi_id = dl_an.loi_id
        AND dl_senat.chambre = 'SENAT'
    """))
    count = result.rowcount
    print(f"[Step 3] Relinked {count} SENAT debates from AN to SENAT dossiers")
    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Fix pre-2002 Sénat dossiers & link orphaned debates")
    print("=" * 60 + "\n")

    session = get_session()

    try:
        # Step 1
        create_senat_dossiers(session)

        # Step 2
        link_orphaned_debates(session)

        # Step 3
        fix_chamber_mismatch(session)

        session.commit()
        print("All changes committed.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    # Summary
    session = get_session()
    total_senat_dossiers = (
        session.query(func.count(DossierLegislatif.id))
        .filter_by(chambre="SENAT")
        .scalar()
    )
    still_orphaned = (
        session.query(func.count(Debat.id))
        .filter(Debat.chambre == "SENAT", Debat.dossier_id.is_(None))
        .scalar()
    )
    session.close()

    print(f"\nFinal state:")
    print(f"  SENAT dossiers total: {total_senat_dossiers}")
    print(f"  Orphaned SENAT debates remaining: {still_orphaned}")


if __name__ == "__main__":
    main()
