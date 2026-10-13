#!/usr/bin/env python3
"""Remove consecutive duplicate interventions (parsing artifacts).

Targets interventions where (debat_id, orateur, contenu) is identical
and ordre values are consecutive (N, N+1). Keeps the first, deletes the second.

Does NOT touch non-consecutive duplicates (legitimate parliamentary repetitions).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from src.db.models import Intervention, get_session


def find_consecutive_duplicates(session):
    """Find intervention IDs that are consecutive duplicates."""
    sql = text("""
        SELECT i2.id, i2.debat_id, i2.orateur, i2.ordre,
               LEFT(i2.contenu, 80) AS contenu_preview
        FROM interventions i1
        JOIN interventions i2
          ON i1.debat_id = i2.debat_id
         AND i1.orateur = i2.orateur
         AND i1.contenu = i2.contenu
         AND i2.ordre = i1.ordre + 1
         AND i1.id != i2.id
        ORDER BY i2.debat_id, i2.ordre
    """)
    return session.execute(sql).fetchall()


def recompact_orders(session, debat_ids):
    """Recompact intervention orders for affected debates."""
    for debat_id in debat_ids:
        interventions = (
            session.query(Intervention)
            .filter_by(debat_id=debat_id)
            .order_by(Intervention.ordre)
            .all()
        )
        for i, interv in enumerate(interventions):
            if interv.ordre != i + 1:
                interv.ordre = i + 1


def main():
    session = get_session()

    # Count before
    total_before = session.query(Intervention).count()
    print(f"Interventions before: {total_before}")

    # Find consecutive duplicates
    duplicates = find_consecutive_duplicates(session)
    print(f"Consecutive duplicates found: {len(duplicates)}")

    if not duplicates:
        print("Nothing to fix.")
        session.close()
        return

    # Show sample
    print("\nSample (first 10):")
    for row in duplicates[:10]:
        print(f"  debat={row.debat_id} ordre={row.ordre} "
              f"orateur={row.orateur[:30]} contenu={row.contenu_preview}...")

    # Collect IDs to delete and affected debates
    ids_to_delete = [row.id for row in duplicates]
    affected_debats = list({row.debat_id for row in duplicates})
    print(f"\nDeleting {len(ids_to_delete)} interventions across {len(affected_debats)} debates...")

    # Delete in batches
    batch_size = 500
    for i in range(0, len(ids_to_delete), batch_size):
        batch = ids_to_delete[i:i + batch_size]
        session.query(Intervention).filter(Intervention.id.in_(batch)).delete(
            synchronize_session=False
        )
    session.flush()

    # Recompact orders
    print(f"Recompacting orders for {len(affected_debats)} debates...")
    recompact_orders(session, affected_debats)

    session.commit()

    # Count after
    total_after = session.query(Intervention).count()
    print(f"\nInterventions after: {total_after}")
    print(f"Removed: {total_before - total_after}")

    # Verify no consecutive duplicates remain
    remaining = find_consecutive_duplicates(session)
    print(f"Remaining consecutive duplicates: {len(remaining)}")

    session.close()


if __name__ == "__main__":
    main()
