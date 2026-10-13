#!/usr/bin/env python3
"""Migration script: flag vote nominal interventions.

Interventions with orateur > 500 chars and fonction='autre' are OCR'd lists
of deputies from nominal votes, not real interventions. Tag them as
'vote_nominaux' in the fonction field.

Usage:
    python scripts/migrate_flag_votes_nominaux.py                  # Dry run
    python scripts/migrate_flag_votes_nominaux.py --apply           # Apply
"""

import argparse
from collections import Counter

import psycopg2


def main():
    parser = argparse.ArgumentParser(description="Flag vote nominaux interventions")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    parser.add_argument("--db", default="postgresql://localhost/legislateur_local", help="DB URL")
    args = parser.parse_args()

    conn = psycopg2.connect(args.db)
    cur = conn.cursor()

    # --- Phase 1: Analyze ---
    print("=" * 70)
    print("PHASE 1: ANALYSE - Orateurs massifs (votes nominaux)")
    print("=" * 70)

    cur.execute("""
        SELECT id, LENGTH(orateur), fonction, LENGTH(contenu), debat_id
        FROM interventions
        WHERE LENGTH(orateur) > 500
        ORDER BY LENGTH(orateur) DESC
    """)
    rows = cur.fetchall()
    print(f"Total interventions avec orateur > 500 chars: {len(rows):,}")

    # Check fonction distribution
    fonctions = Counter(r[2] for r in rows)
    print(f"\nDistribution des fonctions:")
    for fn, count in fonctions.most_common():
        print(f"  \"{fn}\": {count:,}")

    # Only flag those with fonction='autre'
    candidates = [r for r in rows if r[2] == "autre"]
    non_autre = [r for r in rows if r[2] != "autre"]

    print(f"\nCandidats (fonction='autre'): {len(candidates):,}")
    if non_autre:
        print(f"Exclus (autre fonction): {len(non_autre)}")
        for row_id, orat_len, fn, cont_len, debat_id in non_autre[:5]:
            print(f"  id={row_id} | fn={fn} | orateur={orat_len} chars | contenu={cont_len} chars")

    # Contenu length distribution
    cont_lengths = [r[3] for r in candidates]
    if cont_lengths:
        print(f"\nDistribution longueur contenu:")
        print(f"  Min: {min(cont_lengths)}, Max: {max(cont_lengths)}, Avg: {sum(cont_lengths)/len(cont_lengths):.0f}")

    # Debats distribution
    debats = Counter(r[4] for r in candidates)
    print(f"Répartis sur {len(debats)} débats")

    # --- Phase 2: Show samples ---
    print(f"\n{'=' * 70}")
    print(f"PHASE 2: ÉCHANTILLONS ({min(10, len(candidates))} sur {len(candidates):,})")
    print("=" * 70)

    sample_ids = [r[0] for r in candidates[:10]]
    if sample_ids:
        placeholders = ",".join(["%s"] * len(sample_ids))
        cur.execute(f"""
            SELECT id, orateur, contenu
            FROM interventions WHERE id IN ({placeholders})
        """, tuple(sample_ids))
        for row_id, orateur, contenu in cur.fetchall():
            print(f"\n  id={row_id}")
            print(f"    orateur: {(orateur or '')[:100]}...")
            print(f"    contenu: {(contenu or '')[:150]}")

    # --- Phase 3: Verifications ---
    print(f"\n{'=' * 70}")
    print("PHASE 3: VÉRIFICATIONS")
    print("=" * 70)

    issues = []

    # Verify all are fonction='autre'
    non_autre_count = sum(1 for r in rows if r[2] != "autre" and r[1] > 500)
    if non_autre_count:
        issues.append(f"! {non_autre_count} interventions > 500 chars ont une fonction != 'autre'")

    # Check that 'vote_nominaux' doesn't already exist
    cur.execute("SELECT COUNT(*) FROM interventions WHERE fonction = 'vote_nominaux'")
    existing = cur.fetchone()[0]
    if existing:
        issues.append(f"! {existing} interventions ont déjà fonction='vote_nominaux'")

    if not issues:
        print("  Aucun problème détecté")
    else:
        for issue in issues:
            print(f"  {issue}")

    # --- Phase 4: Apply or not ---
    if not args.apply:
        print(f"\n{'=' * 70}")
        print("MODE DRY RUN")
        print(f"Relancer avec --apply pour tagger {len(candidates):,} interventions")
        print("=" * 70)
        conn.close()
        return

    print(f"\n{'=' * 70}")
    print(f"PHASE 4: APPLICATION")
    print("=" * 70)

    cur.execute("""
        UPDATE interventions SET fonction = 'vote_nominaux'
        WHERE LENGTH(orateur) > 500 AND fonction = 'autre'
    """)
    updated = cur.rowcount
    conn.commit()
    print(f"  {updated:,} interventions taguées 'vote_nominaux'")

    # --- Phase 5: Post-apply verification ---
    print(f"\n{'=' * 70}")
    print("PHASE 5: VÉRIFICATION POST-APPLICATION")
    print("=" * 70)

    cur.execute("SELECT COUNT(*) FROM interventions WHERE fonction = 'vote_nominaux'")
    print(f"  fonction='vote_nominaux': {cur.fetchone()[0]:,}")

    cur.execute("SELECT COUNT(*) FROM interventions WHERE LENGTH(orateur) > 500 AND fonction != 'vote_nominaux'")
    print(f"  orateur > 500 chars avec autre fonction: {cur.fetchone()[0]:,}")

    conn.close()
    print("\n  Migration terminée")


if __name__ == "__main__":
    main()
