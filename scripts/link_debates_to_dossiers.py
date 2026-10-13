#!/usr/bin/env python3
"""Link debates to dossiers/laws via content-based matching and date fallback."""

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.import_data import extract_law_reference_from_content
from src.db.models import Debat, DossierLegislatif, Intervention, Loi, get_session


def get_debate_content(session, debat):
    """Get text content for a debate, falling back to interventions if contenu_integral is absent.

    Args:
        session: Database session
        debat: Debat instance

    Returns:
        Text content string (may be empty)
    """
    if debat.contenu_integral:
        return debat.contenu_integral

    # Fall back to concatenated intervention content
    interventions = (
        session.query(Intervention)
        .filter(Intervention.debat_id == debat.id)
        .order_by(Intervention.ordre)
        .all()
    )
    if interventions:
        return "\n".join(i.contenu for i in interventions if i.contenu)

    return ""


def match_debate_by_content(debat_content, laws, dossiers_by_loi, debat_chambre=None):
    """Try to match a debate to a law/dossier based on content references.

    When multiple laws match the same (type, year), prefers the one whose
    dossier chambre matches the debate's chambre.

    Args:
        debat_content: Text content of the debate
        laws: List of all Loi instances
        dossiers_by_loi: Dict mapping loi_id to DossierLegislatif
        debat_chambre: Optional debate chambre for disambiguation

    Returns:
        (Loi, DossierLegislatif) tuple or (None, None) if no match
    """
    if not debat_content:
        return None, None

    law_ref = extract_law_reference_from_content(debat_content)
    if not law_ref:
        return None, None

    law_type, year = law_ref

    # Collect all matching laws (multiple LFRs can share a year)
    candidates = []
    for loi in laws:
        if loi.type == law_type and loi.annee == year:
            dossier = dossiers_by_loi.get(loi.id)
            candidates.append((loi, dossier))

    if not candidates:
        return None, None

    if len(candidates) == 1:
        return candidates[0]

    # Disambiguate: prefer the candidate whose dossier chambre matches the debate
    if debat_chambre:
        for loi, dossier in candidates:
            if dossier and dossier.chambre == debat_chambre:
                return loi, dossier

    # Fallback: return the first candidate with a dossier
    for loi, dossier in candidates:
        if dossier:
            return loi, dossier

    return candidates[0]


def match_debate_by_date(debat, law_configs):
    """Match a debate to laws by date proximity. Returns list of (loi, start, end) matches.

    Args:
        debat: Debat instance with a date
        law_configs: List of (loi, start_date, end_date) tuples

    Returns:
        List of matching (loi, start_date, end_date) tuples
    """
    if not debat.date:
        return []

    return [
        (loi, start, end)
        for loi, start, end in law_configs
        if start <= debat.date <= end
    ]


def disambiguate_by_metadata(debat, date_matches, dossiers_by_loi):
    """When multiple laws match by date, disambiguate using chambre/etape metadata.

    Prioritization:
    1. If debate chambre matches a dossier's chambre, prefer that law
    2. If debate etape contains hints (e.g. "commission"), prefer matching dossier etapes
    3. Prefer LF over LFSS/LFR as a final tiebreaker (LF debates are most common)

    Args:
        debat: Debat instance
        date_matches: List of (loi, start, end) from date matching
        dossiers_by_loi: Dict mapping loi_id to DossierLegislatif

    Returns:
        Best matching Loi or None
    """
    if not date_matches:
        return None

    if len(date_matches) == 1:
        return date_matches[0][0]

    # Score each match
    scored = []
    for loi, start, end in date_matches:
        score = 0
        dossier = dossiers_by_loi.get(loi.id)

        # Prefer matches where the dossier chambre matches the debate chambre
        if dossier and debat.chambre and dossier.chambre == debat.chambre:
            score += 2

        # Prefer LF as the most common type in the Sep-Dec window
        if loi.type == "LF":
            score += 1

        scored.append((score, loi))

    # Sort by score descending, return best
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def link_debates_to_dossiers():
    """Link debates to dossiers using content-based matching first, then date fallback."""

    session = get_session()

    # Get all laws
    laws = session.query(Loi).all()
    print(f"Found {len(laws)} laws in database.\n")

    # Get debates without dossier link (single query)
    unlinked_debates = session.query(Debat).filter(Debat.dossier_id.is_(None)).all()
    print(f"Found {len(unlinked_debates)} debates without dossier link.\n")

    if not unlinked_debates:
        session.close()
        return

    # Pre-load all dossiers grouped by loi_id (single query)
    # Use (loi_id, chambre) key to avoid collision between AN and SENAT dossiers
    all_dossiers = session.query(DossierLegislatif).all()
    dossiers_by_loi = {}
    dossiers_by_loi_chambre = {}
    for d in all_dossiers:
        dossiers_by_loi[d.loi_id] = d  # Last-wins (backward compat for date fallback)
        dossiers_by_loi_chambre[(d.loi_id, d.chambre)] = d

    # Build law windows for date-based fallback
    law_configs = []
    for loi in laws:
        promulgation = loi.date_promulgation
        if loi.type == "LFSS":
            start_date = promulgation - timedelta(days=120)
        elif loi.type in ("LF", "LFR"):
            start_date = promulgation - timedelta(days=90)
        else:
            start_date = promulgation - timedelta(days=60)
        end_date = promulgation + timedelta(days=15)
        law_configs.append((loi, start_date, end_date))

    linked = 0
    dossiers_created = 0
    content_matched = 0
    date_matched = 0

    for debat in unlinked_debates:
        matched_loi = None
        matched_dossier = None

        # Step 1: Try content-based matching
        content = get_debate_content(session, debat)
        matched_loi, matched_dossier = match_debate_by_content(
            content, laws, dossiers_by_loi, debat_chambre=debat.chambre
        )

        if matched_loi:
            content_matched += 1
        else:
            # Step 2: Fall back to date proximity
            date_matches = match_debate_by_date(debat, law_configs)
            if date_matches:
                # Step 3: Disambiguate if multiple matches
                matched_loi = disambiguate_by_metadata(
                    debat, date_matches, dossiers_by_loi
                )
                if matched_loi:
                    matched_dossier = dossiers_by_loi.get(matched_loi.id)
                    date_matched += 1

        if not matched_loi:
            continue

        # Find or create dossier for this law
        if not matched_dossier:
            matched_dossier = DossierLegislatif(
                loi_id=matched_loi.id,
                chambre=debat.chambre or "AN",
                reference_externe=f"dossier_{matched_loi.numero}",
            )
            session.add(matched_dossier)
            session.flush()
            dossiers_by_loi[matched_loi.id] = matched_dossier
            dossiers_created += 1

        debat.dossier_id = matched_dossier.id
        linked += 1

    session.commit()

    print("\n--- Summary ---")
    print(f"Created {dossiers_created} new dossiers")
    print(f"Linked {linked} debates to dossiers")
    print(f"  Content-matched: {content_matched}")
    print(f"  Date-matched:    {date_matched}")

    # Final stats
    total_debates = session.query(Debat).count()
    linked_debates = session.query(Debat).filter(Debat.dossier_id.isnot(None)).count()
    unlinked_remaining = total_debates - linked_debates

    print(
        f"\nDebates: {linked_debates} linked, {unlinked_remaining} unlinked,"
        f" {total_debates} total"
    )

    session.close()


if __name__ == "__main__":
    print("Linking debates to dossiers via content + date matching...\n")
    link_debates_to_dossiers()
