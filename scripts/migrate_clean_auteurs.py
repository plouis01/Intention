#!/usr/bin/env python3
"""Migration script: normalize amendment auteur field.

Applies normalize_amendment_author() to all existing amendements in the DB,
removing multi-signatory lists and normalizing Gouvernement variants.

Usage:
    python scripts/migrate_clean_auteurs.py                  # Dry run (default)
    python scripts/migrate_clean_auteurs.py --apply           # Apply changes
    python scripts/migrate_clean_auteurs.py --apply --batch=5000  # Custom batch size
"""

import argparse
import sys
from collections import Counter

import psycopg2

from src.parsers.amendments import normalize_amendment_author


def main():
    parser = argparse.ArgumentParser(description="Normalize amendment auteur field")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    parser.add_argument("--db", default="postgresql://localhost/legislateur_local", help="DB URL")
    parser.add_argument("--batch", type=int, default=5000, help="Batch size for updates")
    parser.add_argument("--show-samples", type=int, default=30, help="Number of samples to show")
    args = parser.parse_args()

    conn = psycopg2.connect(args.db)
    cur = conn.cursor()

    # --- Phase 1: Analyze ---
    print("=" * 70)
    print("PHASE 1: ANALYSE")
    print("=" * 70)

    cur.execute("SELECT COUNT(*) FROM amendements")
    total = cur.fetchone()[0]
    print(f"Total amendements: {total:,}")

    cur.execute("SELECT COUNT(*) FROM amendements WHERE auteur IS NOT NULL")
    with_auteur = cur.fetchone()[0]
    print(f"Avec auteur non-null: {with_auteur:,}")

    # Fetch all auteur values
    cur.execute("SELECT id, auteur FROM amendements WHERE auteur IS NOT NULL")
    rows = cur.fetchall()

    # Compute changes
    changes = []  # (id, old_auteur, new_auteur)
    unchanged = 0
    stats = Counter()

    for row_id, old_auteur in rows:
        new_auteur = normalize_amendment_author(old_auteur)
        if new_auteur != old_auteur:
            changes.append((row_id, old_auteur, new_auteur))
            # Classify the change
            if old_auteur and new_auteur == "Gouvernement":
                stats["gouvernement_normalized"] += 1
            elif old_auteur and len(old_auteur) > 100:
                stats["long_list_shortened"] += 1
            elif old_auteur and "," in old_auteur:
                stats["comma_list_shortened"] += 1
            else:
                stats["other"] += 1
        else:
            unchanged += 1

    print(f"\nRésultats:")
    print(f"  Inchangés:  {unchanged:,}")
    print(f"  À modifier: {len(changes):,}")
    print(f"\nDétail des modifications:")
    for category, count in stats.most_common():
        print(f"  {category}: {count:,}")

    # --- Phase 2: Show samples ---
    print(f"\n{'=' * 70}")
    print(f"PHASE 2: ÉCHANTILLONS ({min(args.show_samples, len(changes))} sur {len(changes):,})")
    print("=" * 70)

    # Show diverse samples: longest, shortest, and random
    if changes:
        # Sort by old_auteur length descending for diversity
        by_length = sorted(changes, key=lambda x: len(x[1] or ""), reverse=True)

        shown = set()
        samples = []

        # Top 10 longest
        for item in by_length[:10]:
            if item[0] not in shown:
                samples.append(item)
                shown.add(item[0])

        # Gouvernement samples
        for item in changes:
            if len(samples) >= args.show_samples:
                break
            if item[2] == "Gouvernement" and item[0] not in shown:
                samples.append(item)
                shown.add(item[0])

        # Name+function samples (comma but kept)
        for item in changes:
            if len(samples) >= args.show_samples:
                break
            if item[2] and "," in item[2] and item[0] not in shown:
                samples.append(item)
                shown.add(item[0])

        # Fill rest with shortest changes
        for item in reversed(by_length):
            if len(samples) >= args.show_samples:
                break
            if item[0] not in shown:
                samples.append(item)
                shown.add(item[0])

        for i, (row_id, old, new) in enumerate(samples):
            print(f"\n--- Sample {i+1} (id={row_id}) ---")
            print(f"  AVANT [{len(old or ''):3d}]: {(old or '')[:150]}")
            print(f"  APRÈS [{len(new or ''):3d}]: {new}")

    # --- Phase 3: Verification checks ---
    print(f"\n{'=' * 70}")
    print("PHASE 3: VÉRIFICATIONS")
    print("=" * 70)

    # Check for potential issues
    issues = []

    # 1. Check that no auteur becomes empty after normalization
    empty_after = [(r[0], r[1]) for r in changes if not r[2]]
    if empty_after:
        issues.append(f"⚠ {len(empty_after)} auteurs deviendraient None/vide après normalisation")
        for row_id, old in empty_after[:5]:
            print(f"    id={row_id}: '{old[:80]}'")

    # 2. Check that Gouvernement is normalized consistently
    gov_values = Counter()
    for _, old, new in changes:
        if new == "Gouvernement":
            gov_values[old.strip()] += 1
    if gov_values:
        print(f"\nGouvernement - variantes normalisées:")
        for variant, count in gov_values.most_common():
            print(f"    '{variant[:60]}' × {count}")

    # 3. Check new auteur length distribution
    new_lengths = [len(new or "") for _, _, new in changes]
    if new_lengths:
        print(f"\nDistribution longueur APRÈS normalisation:")
        print(f"  Min: {min(new_lengths)}, Max: {max(new_lengths)}, Avg: {sum(new_lengths)/len(new_lengths):.1f}")
        long_after = [c for c in changes if c[2] and len(c[2]) > 100]
        if long_after:
            issues.append(f"⚠ {len(long_after)} auteurs restent > 100 chars après normalisation")
            for row_id, old, new in long_after[:5]:
                print(f"    id={row_id}: '{new[:120]}'")

    # 4. Check that name+function cases are preserved correctly
    with_function = [(r[0], r[1], r[2]) for r in changes if r[2] and "rapporteur" in r[2].lower()]
    if with_function:
        print(f"\nNom+fonction préservés ({len(with_function)} cas):")
        for row_id, old, new in with_function[:5]:
            print(f"    id={row_id}: '{new[:120]}'")

    if not issues:
        print("\n✓ Aucun problème détecté")
    else:
        print(f"\n{'!' * 50}")
        for issue in issues:
            print(f"  {issue}")
        print(f"{'!' * 50}")

    # --- Phase 4: Apply or not ---
    if not args.apply:
        print(f"\n{'=' * 70}")
        print("MODE DRY RUN - aucune modification effectuée")
        print(f"Relancer avec --apply pour appliquer les {len(changes):,} modifications")
        print("=" * 70)
        conn.close()
        return

    if not changes:
        print("\nAucune modification nécessaire.")
        conn.close()
        return

    print(f"\n{'=' * 70}")
    print(f"PHASE 4: APPLICATION ({len(changes):,} modifications)")
    print("=" * 70)

    # Apply in batches
    applied = 0
    for i in range(0, len(changes), args.batch):
        batch = changes[i:i + args.batch]
        for row_id, _, new_auteur in batch:
            cur.execute(
                "UPDATE amendements SET auteur = %s WHERE id = %s",
                (new_auteur, row_id)
            )
        conn.commit()
        applied += len(batch)
        print(f"  Batch {i // args.batch + 1}: {applied:,} / {len(changes):,} appliqués")

    print(f"\n✓ {applied:,} amendements mis à jour")

    # --- Phase 5: Post-apply verification ---
    print(f"\n{'=' * 70}")
    print("PHASE 5: VÉRIFICATION POST-APPLICATION")
    print("=" * 70)

    cur.execute("""
        SELECT
            CASE
                WHEN LENGTH(auteur) <= 30 THEN '0-30 (normal)'
                WHEN LENGTH(auteur) <= 80 THEN '31-80 (borderline)'
                WHEN LENGTH(auteur) <= 200 THEN '81-200 (multi-author)'
                ELSE '200+ (massive list)'
            END as length_range,
            COUNT(*) as count
        FROM amendements WHERE auteur IS NOT NULL
        GROUP BY length_range
        ORDER BY MIN(LENGTH(auteur))
    """)
    print("Distribution longueur auteur:")
    for row in cur.fetchall():
        print(f"  {row[0]:30s}: {row[1]:,}")

    cur.execute("SELECT auteur, LENGTH(auteur) FROM amendements WHERE auteur IS NOT NULL ORDER BY LENGTH(auteur) DESC LIMIT 10")
    print("\nTop 10 plus longs:")
    for auteur, length in cur.fetchall():
        print(f"  [{length:3d}] {(auteur or '')[:100]}")

    conn.close()
    print("\n✓ Migration terminée")


if __name__ == "__main__":
    main()
