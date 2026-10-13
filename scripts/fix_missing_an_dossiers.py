#!/usr/bin/env python3
"""Create missing AN dossiers for laws that don't have one.

6 laws in the PG database lack an AN DossierLegislatif:
- LF 2012 (2011-1977)
- LFR 2012 (2012-354)
- LFR 2012 (2012-958)
- LF 2014 (2013-1278)
- LF 2018 (2017-1837)
- LF 2019 (2018-1317)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.models import DossierLegislatif, Loi, get_session


def fix_missing_an_dossiers():
    """Create AN dossiers for laws that are missing them."""
    session = get_session()

    # Find all lois
    lois = session.query(Loi).all()
    print(f"Total lois: {len(lois)}")

    created = 0
    already_exist = 0

    for loi in lois:
        # Check if this law has an AN dossier
        an_dossier = (
            session.query(DossierLegislatif)
            .filter_by(loi_id=loi.id, chambre="AN")
            .first()
        )

        if an_dossier:
            already_exist += 1
            continue

        # Create missing AN dossier
        dossier = DossierLegislatif(
            loi_id=loi.id,
            chambre="AN",
        )
        session.add(dossier)
        created += 1
        print(f"  + Created AN dossier for loi {loi.numero} ({loi.titre[:50] if loi.titre else 'N/A'})")

    session.commit()

    # Summary
    print(f"\n--- Summary ---")
    print(f"Already had AN dossier: {already_exist}")
    print(f"Created: {created}")

    total_dossiers = session.query(DossierLegislatif).count()
    an_count = session.query(DossierLegislatif).filter_by(chambre="AN").count()
    senat_count = session.query(DossierLegislatif).filter_by(chambre="SENAT").count()
    print(f"Total dossiers: {total_dossiers} (AN: {an_count}, SENAT: {senat_count})")

    session.close()


if __name__ == "__main__":
    print("Fixing missing AN dossiers...\n")
    fix_missing_an_dossiers()
