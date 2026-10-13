"""Phase 5: Fix president false speakers (Macron, Sarkozy, Chirac, Hollande).

The parser splits interventions whenever it encounters "M. Macron" (or other
president names) in the text, creating false speaker attributions. This script
merges those fragments back into the real speaker's intervention.

Handles chains (multiple consecutive false splits from the same speech) by
walking back to the first non-president intervention.
"""
from sqlalchemy import create_engine, text

engine = create_engine('postgresql://localhost/legislateur_local')

PRESIDENT_FALSE_SPEAKERS = [
    ("M. Macron", "2017-05-14"),
    ("M. Sarkozy", "2007-05-16"),
    ("M. Chirac", "1995-05-17"),
    ("M. Hollande", "2012-05-15"),
]

# Collect all president names for chain detection
PRESIDENT_NAMES = {name for name, _ in PRESIDENT_FALSE_SPEAKERS}

with engine.begin() as conn:
    total = 0
    for name, date_from in PRESIDENT_FALSE_SPEAKERS:
        # Get all false interventions with the actual previous intervention
        # (using ORDER BY ordre DESC LIMIT 1 instead of ordre - 1 to handle gaps)
        results = conn.execute(text("""
            SELECT i.id, i.ordre, i.orateur, i.contenu, i.debat_id,
                   prev.id as prev_id, prev.ordre as prev_ordre,
                   prev.orateur as prev_orateur, prev.contenu as prev_contenu
            FROM interventions i
            JOIN debats d ON i.debat_id = d.id
            JOIN LATERAL (
                SELECT p.id, p.ordre, p.orateur, p.contenu
                FROM interventions p
                WHERE p.debat_id = i.debat_id AND p.ordre < i.ordre
                ORDER BY p.ordre DESC LIMIT 1
            ) prev ON true
            WHERE i.orateur = :name AND d.date > :date_from
            ORDER BY i.debat_id, i.ordre
        """), {"name": name, "date_from": date_from}).fetchall()

        # Group by debat to handle chains correctly
        debat_groups = {}
        for row in results:
            fid, ordre, orateur, contenu, debat_id, prev_id, prev_ordre, prev_orateur, prev_contenu = row
            debat_groups.setdefault(debat_id, []).append({
                "id": fid, "ordre": ordre, "orateur": orateur, "contenu": contenu,
                "prev_id": prev_id, "prev_ordre": prev_ordre,
                "prev_orateur": prev_orateur, "prev_contenu": prev_contenu,
            })

        deleted_ids = []
        for debat_id, entries in debat_groups.items():
            # Sort by ordre to process in order
            entries.sort(key=lambda e: e["ordre"])

            for entry in entries:
                if entry["id"] in deleted_ids:
                    continue

                # Walk back the chain to find the real (non-president) speaker
                chain = [entry]
                target_id = entry["prev_id"]
                target_orateur = entry["prev_orateur"]

                # If prev is also a false president speaker, keep walking back
                while target_orateur in PRESIDENT_NAMES:
                    prev_of_prev = conn.execute(text("""
                        SELECT p.id, p.orateur, p.contenu
                        FROM interventions p
                        WHERE p.debat_id = :debat_id AND p.id = :pid
                    """), {"debat_id": debat_id, "pid": target_id}).fetchone()

                    if not prev_of_prev:
                        break

                    # Find the prev of this prev
                    prev_prev = conn.execute(text("""
                        SELECT p.id, p.orateur
                        FROM interventions p
                        WHERE p.debat_id = :debat_id AND p.ordre < (
                            SELECT ordre FROM interventions WHERE id = :pid
                        )
                        ORDER BY p.ordre DESC LIMIT 1
                    """), {"debat_id": debat_id, "pid": target_id}).fetchone()

                    if not prev_prev:
                        break

                    # The current target is also a false speaker, add to chain
                    deleted_ids.append(target_id)
                    chain.insert(0, {
                        "id": target_id,
                        "orateur": prev_of_prev[1],
                        "contenu": prev_of_prev[2],
                    })
                    target_id = prev_prev[0]
                    target_orateur = prev_prev[1]

                # Build merged content: target_contenu + " M. X " + chain[0] + " M. X " + chain[1] + ...
                # Fetch current target content (may have been updated by a previous merge)
                target_row = conn.execute(text(
                    "SELECT contenu FROM interventions WHERE id = :pid"
                ), {"pid": target_id}).fetchone()

                if not target_row:
                    continue

                merged = target_row[0]
                for c in chain:
                    merged = merged + " " + c["orateur"] + " " + c["contenu"]

                conn.execute(text(
                    "UPDATE interventions SET contenu = :merged WHERE id = :pid"
                ), {"merged": merged.strip(), "pid": target_id})
                deleted_ids.append(entry["id"])

        if deleted_ids:
            # Deduplicate
            deleted_ids = list(set(deleted_ids))
            # Remove associated chunk_embeddings first (FK constraint)
            conn.execute(text(
                "DELETE FROM chunk_embeddings WHERE intervention_id = ANY(:ids)"
            ), {"ids": deleted_ids})
            conn.execute(text(
                "DELETE FROM interventions WHERE id = ANY(:ids)"
            ), {"ids": deleted_ids})

        print(f"Fixed {len(deleted_ids)} false '{name}' interventions")
        total += len(deleted_ids)

    print(f"Total: {total}")
