#!/usr/bin/env python3
"""Migration script: nullify truncated fonction values in interventions.

Parsing errors left short garbage values like "M", "MM", ". ", ", " in the
fonction field. These are not exploitable - set them to NULL.

Usage:
    python scripts/migrate_fix_fonction_tronquee.py                  # Dry run
    python scripts/migrate_fix_fonction_tronquee.py --apply           # Apply
"""

import argparse
from collections import Counter

import psycopg2


# Fonction values that are clearly truncated/garbage
_GARBAGE_FONCTIONS = {"M", "MM", "m", ". ", ", ", "(", "co", "pré", "pr", "cor", "à M"}


def main():
    parser = argparse.ArgumentParser(description="Nullify truncated fonction values")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    parser.add_argument("--db", default="postgresql://localhost/legislateur_local", help="DB URL")
    args = parser.parse_args()

    conn = psycopg2.connect(args.db)
    cur = conn.cursor()

    # --- Phase 1: Analyze ---
    print("=" * 70)
    print("PHASE 1: ANALYSE - Fonctions tronquées")
    print("=" * 70)

    cur.execute("""
        SELECT fonction, COUNT(*) FROM interventions
        WHERE LENGTH(fonction) <= 3 AND fonction IS NOT NULL
        GROUP BY fonction ORDER BY COUNT(*) DESC
    """)
    rows = cur.fetchall()
    print("Fonctions <= 3 chars:")
    total_candidates = 0
    for fn, count in rows:
        is_garbage = fn in _GARBAGE_FONCTIONS
        marker = " <- GARBAGE" if is_garbage else ""
        print(f"  \"{fn}\": {count:,}{marker}")
        if is_garbage:
            total_candidates += count

    print(f"\nTotal à nullifier: {total_candidates:,}")

    # --- Phase 2: Fetch and show samples ---
    print(f"\n{'=' * 70}")
    print("PHASE 2: ÉCHANTILLONS")
    print("=" * 70)

    placeholders = ",".join(["%s"] * len(_GARBAGE_FONCTIONS))
    cur.execute(f"""
        SELECT id, orateur, fonction
        FROM interventions
        WHERE fonction IN ({placeholders})
        ORDER BY fonction, id
        LIMIT 30
    """, tuple(_GARBAGE_FONCTIONS))

    current_fn = None
    for row_id, orateur, fn in cur.fetchall():
        if fn != current_fn:
            print(f"\n  fonction = \"{fn}\":")
            current_fn = fn
        print(f"    id={row_id} | {(orateur or '')[:40]}")

    # --- Phase 3: Verifications ---
    print(f"\n{'=' * 70}")
    print("PHASE 3: VÉRIFICATIONS")
    print("=" * 70)

    issues = []

    # Check that orateur is always set (we're not losing info)
    cur.execute(f"""
        SELECT COUNT(*) FROM interventions
        WHERE fonction IN ({placeholders}) AND orateur IS NULL
    """, tuple(_GARBAGE_FONCTIONS))
    null_orateur = cur.fetchone()[0]
    if null_orateur:
        issues.append(f"! {null_orateur} lignes ont orateur NULL (perte d'info si on nullifie fonction)")
    else:
        print("  Tous les orateurs sont renseignés (OK)")

    # Pre-count of NULL fonctions
    cur.execute("SELECT COUNT(*) FROM interventions WHERE fonction IS NULL")
    null_before = cur.fetchone()[0]
    print(f"  fonction IS NULL avant: {null_before:,}")
    print(f"  fonction IS NULL après (attendu): {null_before + total_candidates:,} (+{total_candidates})")

    if not issues:
        print("  Aucun problème détecté")
    else:
        for issue in issues:
            print(f"  {issue}")

    # --- Phase 4: Apply or not ---
    if not args.apply:
        print(f"\n{'=' * 70}")
        print("MODE DRY RUN")
        print(f"Relancer avec --apply pour nullifier {total_candidates:,} fonctions")
        print("=" * 70)
        conn.close()
        return

    print(f"\n{'=' * 70}")
    print(f"PHASE 4: APPLICATION")
    print("=" * 70)

    cur.execute(f"""
        UPDATE interventions SET fonction = NULL
        WHERE fonction IN ({placeholders})
    """, tuple(_GARBAGE_FONCTIONS))
    updated = cur.rowcount
    conn.commit()
    print(f"  {updated:,} interventions mises à jour")

    # --- Phase 5: Post-apply verification ---
    print(f"\n{'=' * 70}")
    print("PHASE 5: VÉRIFICATION POST-APPLICATION")
    print("=" * 70)

    cur.execute(f"""
        SELECT COUNT(*) FROM interventions WHERE fonction IN ({placeholders})
    """, tuple(_GARBAGE_FONCTIONS))
    remaining = cur.fetchone()[0]
    print(f"  Fonctions garbage restantes: {remaining}")

    cur.execute("SELECT COUNT(*) FROM interventions WHERE fonction IS NULL")
    null_after = cur.fetchone()[0]
    print(f"  fonction IS NULL après: {null_after:,}")

    cur.execute("""
        SELECT fonction, COUNT(*) FROM interventions
        WHERE LENGTH(fonction) <= 5 AND fonction IS NOT NULL
        GROUP BY fonction ORDER BY COUNT(*) DESC LIMIT 10
    """)
    print("\nFonctions courtes restantes:")
    for fn, count in cur.fetchall():
        print(f"  \"{fn}\": {count:,}")

    conn.close()
    print("\n  Migration terminée")


if __name__ == "__main__":
    main()
