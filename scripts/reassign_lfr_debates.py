#!/usr/bin/env python3
"""Reassign debates that were misclassified as LF when they should be LFR.

The extract_law_reference_from_content() bug caused LFR debates to be
assigned to LF dossiers (PLF matched before PLFR). This script:

1. Finds LFR laws with 0 debates
2. Checks debates in the corresponding LF dossiers of the same year
3. Re-analyzes content to determine if debate is actually about a LFR
4. Moves correctly identified LFR debates to the right dossier

Usage:
    python scripts/reassign_lfr_debates.py          # Dry run
    python scripts/reassign_lfr_debates.py --apply   # Apply changes
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.import_data import extract_law_reference_from_content
from src.db.models import Debat, DossierLegislatif, Intervention, Loi, get_session


def get_debate_text(session, debat, max_chars=50000):
    """Get text for content analysis."""
    if debat.contenu_integral:
        return debat.contenu_integral[:max_chars]
    rows = (
        session.query(Intervention.contenu)
        .filter(Intervention.debat_id == debat.id)
        .order_by(Intervention.ordre)
        .limit(50)
        .all()
    )
    return "\n".join(contenu for (contenu,) in rows if contenu)[:max_chars]


def has_rectificatif_context(text):
    """Check if debate text discusses a loi de finances rectificative."""
    text_lower = text.lower()
    patterns = [
        r"rectificati",
        r"plfr\b",
        r"projet de loi de finances rectificative",
        r"budget rectificatif",
        r"collectif budgétaire",
    ]
    return any(re.search(p, text_lower) for p in patterns)


def main():
    parser = argparse.ArgumentParser(description="Reassign misclassified LFR debates")
    parser.add_argument("--apply", action="store_true", help="Apply changes")
    args = parser.parse_args()

    session = get_session()
    session.get_bind().echo = False

    # Find all laws grouped by (type, year)
    all_lois = session.query(Loi).all()
    lois_by_key = {}
    for loi in all_lois:
        key = (loi.type, loi.annee)
        if key not in lois_by_key:
            lois_by_key[key] = []
        lois_by_key[key].append(loi)

    # Find LFR laws with 0 debates
    lfr_laws_no_debates = []
    for loi in all_lois:
        if loi.type != "LFR":
            continue
        dossiers = session.query(DossierLegislatif).filter_by(loi_id=loi.id).all()
        total = sum(
            session.query(Debat).filter_by(dossier_id=d.id).count()
            for d in dossiers
        )
        if total == 0:
            lfr_laws_no_debates.append(loi)

    print(f"Found {len(lfr_laws_no_debates)} LFR laws with 0 debates:\n")
    for loi in lfr_laws_no_debates:
        print(f"  {loi.type} {loi.annee} ({loi.numero})")

    if not lfr_laws_no_debates:
        print("Nothing to do.")
        session.close()
        return

    # Group LFR laws by year for disambiguation
    lfr_by_year = {}
    for loi in lfr_laws_no_debates:
        if loi.annee not in lfr_by_year:
            lfr_by_year[loi.annee] = []
        lfr_by_year[loi.annee].append(loi)

    # Collect all candidate moves: debate_id -> (debat, best_lfr_loi, lf_loi)
    # This prevents double-assignment when multiple LFRs share a year
    candidate_moves = {}  # debate_id -> (debat, lfr_loi, lf_loi)
    total_checked = 0

    for year, lfr_lois_for_year in sorted(lfr_by_year.items()):
        # Get all LF laws for this year
        lf_lois = lois_by_key.get(("LF", year), [])
        # Also check year-1 LF (LFR for 2012 debates might be in LF 2012 dossier
        # which is annee_budgetaire=2012 but promulgated in 2011)
        lf_lois_prev = lois_by_key.get(("LF", year - 1), [])

        all_lf_to_check = lf_lois + lf_lois_prev
        if not all_lf_to_check:
            print(f"\n  No LF law found for year {year}/{year-1}, skipping")
            continue

        # Check debates in all LF dossiers
        for lf_loi in all_lf_to_check:
            lf_dossiers = session.query(DossierLegislatif).filter_by(loi_id=lf_loi.id).all()

            for lf_dossier in lf_dossiers:
                debates = session.query(Debat).filter_by(dossier_id=lf_dossier.id).all()

                for debat in debates:
                    if debat.id in candidate_moves:
                        continue  # Already identified
                    total_checked += 1
                    text = get_debate_text(session, debat)
                    if not text:
                        continue

                    # Use the fixed extraction function
                    law_ref = extract_law_reference_from_content(text)
                    if not law_ref:
                        continue

                    ref_type, ref_year = law_ref
                    if ref_type != "LFR" or ref_year != year:
                        continue

                    # Also verify with rectificatif context
                    if not has_rectificatif_context(text):
                        continue

                    # Pick the best LFR: closest promulgation date to the debate
                    if len(lfr_lois_for_year) == 1:
                        best_lfr = lfr_lois_for_year[0]
                    else:
                        # Sort by date proximity to debate
                        debate_date = debat.date
                        if debate_date:
                            best_lfr = min(
                                lfr_lois_for_year,
                                key=lambda l: abs((l.date_promulgation - debate_date).days)
                                if l.date_promulgation else 9999,
                            )
                        else:
                            best_lfr = lfr_lois_for_year[0]

                    candidate_moves[debat.id] = (debat, best_lfr, lf_loi)

    # Apply moves
    total_moved = 0
    for debat_id, (debat, lfr_loi, lf_loi) in sorted(candidate_moves.items()):
        # Get or create LFR dossier
        chambre = debat.chambre or "AN"
        lfr_dossier = (
            session.query(DossierLegislatif)
            .filter_by(loi_id=lfr_loi.id, chambre=chambre)
            .first()
        )

        if not lfr_dossier and args.apply:
            lfr_dossier = DossierLegislatif(
                loi_id=lfr_loi.id,
                chambre=chambre,
                reference_externe=f"dossier_{lfr_loi.numero}_{chambre}",
            )
            session.add(lfr_dossier)
            session.flush()

        date_str = debat.date.strftime("%Y-%m-%d") if debat.date else "?"
        print(
            f"  MOVE: debate {debat.id} ({date_str}, {debat.chambre}) "
            f"from LF {lf_loi.numero} → LFR {lfr_loi.numero}"
        )

        if args.apply and lfr_dossier:
            debat.dossier_id = lfr_dossier.id

        total_moved += 1

    if args.apply:
        session.commit()

    print(f"\n--- Summary ---")
    print(f"  Debates checked:  {total_checked}")
    print(f"  Debates moved:    {total_moved}")

    if not args.apply and total_moved > 0:
        print(f"\nDRY RUN: Use --apply to save changes.")

    # Show updated counts
    if args.apply or True:
        print(f"\nUpdated LFR debate counts:")
        for lfr_loi in lfr_laws_no_debates:
            dossiers = session.query(DossierLegislatif).filter_by(loi_id=lfr_loi.id).all()
            total = sum(
                session.query(Debat).filter_by(dossier_id=d.id).count()
                for d in dossiers
            )
            marker = "" if total > 0 else " ** STILL 0 **"
            print(f"  {lfr_loi.type} {lfr_loi.annee} ({lfr_loi.numero}): {total} debates{marker}")

    session.close()


if __name__ == "__main__":
    main()
