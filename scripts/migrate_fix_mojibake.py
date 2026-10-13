#!/usr/bin/env python3
"""Migration script: fix mojibake (double-encoded UTF-8) in interventions and debats.

A single debate (debat_id=22285, AN 2013-05-22) has 154 interventions with
double-encoded UTF-8 (latin1 interpreted as UTF-8). The contenu and orateur
fields contain patterns like "Ã©" instead of "é".

Usage:
    python scripts/migrate_fix_mojibake.py                  # Dry run (default)
    python scripts/migrate_fix_mojibake.py --apply           # Apply changes
"""

import argparse

import psycopg2


def fix_mojibake(text: str | None) -> str | None:
    """Fix double-encoded UTF-8 text.

    Attempts to encode as latin1 then decode as UTF-8.
    Returns fixed text if mojibake was detected, None otherwise.
    """
    if not text or "Ã" not in text:
        return None
    try:
        fixed = text.encode("latin1").decode("utf-8")
        if fixed != text:
            return fixed
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description="Fix mojibake in interventions and debats")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    parser.add_argument("--db", default="postgresql://localhost/legislateur_local", help="DB URL")
    args = parser.parse_args()

    conn = psycopg2.connect(args.db)
    cur = conn.cursor()

    # --- Phase 1: Detect affected interventions ---
    print("=" * 70)
    print("PHASE 1: DÉTECTION - Interventions avec mojibake")
    print("=" * 70)

    cur.execute("SELECT COUNT(*) FROM interventions")
    print(f"Total interventions: {cur.fetchone()[0]:,}")

    cur.execute("""
        SELECT id, debat_id, orateur, fonction, contenu
        FROM interventions
        WHERE contenu LIKE '%Ã%' OR orateur LIKE '%Ã%' OR fonction LIKE '%Ã%'
    """)
    rows = cur.fetchall()
    print(f"Candidats (pré-filtre Ã): {len(rows)}")

    changes = []  # (id, field_updates: dict)
    for row_id, debat_id, orateur, fonction, contenu in rows:
        updates = {}
        fixed_contenu = fix_mojibake(contenu)
        if fixed_contenu is not None:
            updates["contenu"] = fixed_contenu
        fixed_orateur = fix_mojibake(orateur)
        if fixed_orateur is not None:
            updates["orateur"] = fixed_orateur
        fixed_fonction = fix_mojibake(fonction)
        if fixed_fonction is not None:
            updates["fonction"] = fixed_fonction
        if updates:
            changes.append((row_id, debat_id, orateur, contenu, updates))

    print(f"À modifier: {len(changes)}")

    fields_count = {"contenu": 0, "orateur": 0, "fonction": 0}
    debats_affected = set()
    for row_id, debat_id, _, _, updates in changes:
        debats_affected.add(debat_id)
        for field in updates:
            fields_count[field] += 1

    print(f"\nChamps touchés:")
    for field, count in fields_count.items():
        print(f"  {field}: {count}")
    print(f"Débats concernés: {debats_affected}")

    # --- Phase 2: Detect affected debats ---
    print(f"\n{'=' * 70}")
    print("PHASE 2: DÉTECTION - Débats avec mojibake")
    print("=" * 70)

    cur.execute("""
        SELECT id, chambre, date, LEFT(contenu_integral, 200)
        FROM debats
        WHERE contenu_integral LIKE '%Ã%'
    """)
    debat_rows = cur.fetchall()
    print(f"Débats avec mojibake dans contenu_integral: {len(debat_rows)}")

    debat_changes = []
    for debat_id, chambre, date, preview in debat_rows:
        cur.execute("SELECT contenu_integral FROM debats WHERE id = %s", (debat_id,))
        full_text = cur.fetchone()[0]
        fixed = fix_mojibake(full_text)
        if fixed is not None:
            debat_changes.append((debat_id, chambre, date, full_text[:100], fixed[:100]))
            print(f"  debat_id={debat_id} | {chambre} | {date}")
            print(f"    AVANT: {full_text[:100]}")
            print(f"    APRÈS: {fixed[:100]}")

    # --- Phase 3: Show samples ---
    print(f"\n{'=' * 70}")
    print(f"PHASE 3: ÉCHANTILLONS ({min(20, len(changes))} sur {len(changes)})")
    print("=" * 70)

    for row_id, debat_id, old_orateur, old_contenu, updates in changes[:20]:
        print(f"\n--- id={row_id} debat={debat_id} ---")
        if "orateur" in updates:
            print(f"  orateur AVANT: {old_orateur[:80]}")
            print(f"  orateur APRÈS: {updates['orateur'][:80]}")
        if "contenu" in updates:
            print(f"  contenu AVANT: {old_contenu[:100]}")
            print(f"  contenu APRÈS: {updates['contenu'][:100]}")

    # --- Phase 4: Verifications ---
    print(f"\n{'=' * 70}")
    print("PHASE 4: VÉRIFICATIONS")
    print("=" * 70)

    issues = []

    # Check no content becomes empty
    empty_after = [c for c in changes if any(not v for v in c[4].values())]
    if empty_after:
        issues.append(f"! {len(empty_after)} champs deviendraient vides")

    # Check idempotency
    double_fix_count = 0
    for _, _, _, _, updates in changes[:50]:
        for field, value in updates.items():
            second_fix = fix_mojibake(value)
            if second_fix is not None:
                double_fix_count += 1
    if double_fix_count:
        issues.append(f"! {double_fix_count} champs changent après double application (non-idempotent)")
    else:
        print("  Idempotence OK (double application ne change rien)")

    if not issues:
        print("  Aucun probleme detecte")
    else:
        for issue in issues:
            print(f"  {issue}")

    # --- Phase 5: Apply or not ---
    if not args.apply:
        print(f"\n{'=' * 70}")
        print("MODE DRY RUN")
        print(f"Relancer avec --apply pour appliquer {len(changes)} modifications interventions + {len(debat_changes)} debats")
        print("=" * 70)
        conn.close()
        return

    print(f"\n{'=' * 70}")
    print(f"PHASE 5: APPLICATION")
    print("=" * 70)

    # Apply intervention changes
    for row_id, _, _, _, updates in changes:
        set_clauses = ", ".join(f"{field} = %s" for field in updates)
        values = list(updates.values()) + [row_id]
        cur.execute(f"UPDATE interventions SET {set_clauses} WHERE id = %s", values)

    # Apply debat changes
    for debat_id, _, _, _, _ in debat_changes:
        cur.execute("SELECT contenu_integral FROM debats WHERE id = %s", (debat_id,))
        full_text = cur.fetchone()[0]
        fixed = fix_mojibake(full_text)
        if fixed:
            cur.execute("UPDATE debats SET contenu_integral = %s WHERE id = %s", (fixed, debat_id))

    conn.commit()
    print(f"  {len(changes)} interventions mises a jour")
    print(f"  {len(debat_changes)} debats mis a jour")

    # --- Phase 6: Post-apply verification ---
    print(f"\n{'=' * 70}")
    print("PHASE 6: VERIFICATION POST-APPLICATION")
    print("=" * 70)

    cur.execute("SELECT COUNT(*) FROM interventions WHERE contenu LIKE '%Ã©%' OR contenu LIKE '%Ã¨%'")
    print(f"  Interventions avec mojibake contenu residuel: {cur.fetchone()[0]}")

    cur.execute("SELECT COUNT(*) FROM interventions WHERE orateur LIKE '%Ã%'")
    print(f"  Interventions avec mojibake orateur residuel: {cur.fetchone()[0]}")

    cur.execute("SELECT COUNT(*) FROM debats WHERE contenu_integral LIKE '%Ã©%'")
    print(f"  Debats avec mojibake residuel: {cur.fetchone()[0]}")

    # Show first 5 fixed interventions
    cur.execute("""
        SELECT id, orateur, LEFT(contenu, 120) FROM interventions
        WHERE debat_id = 22285 ORDER BY id LIMIT 5
    """)
    print("\nPremières interventions du debat 22285 (post-fix):")
    for row_id, orateur, contenu in cur.fetchall():
        print(f"  id={row_id} | {orateur[:30]} | {contenu[:80]}")

    conn.close()
    print("\n  Migration terminee")


if __name__ == "__main__":
    main()
