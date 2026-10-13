#!/usr/bin/env python3
"""Migration script: normalize 'la commission' author variants in amendements.

157 different forms exist for commission-authored amendments. This script
normalizes them into clean canonical forms.

Usage:
    python scripts/migrate_clean_auteur_commission.py                  # Dry run
    python scripts/migrate_clean_auteur_commission.py --apply           # Apply
"""

import argparse
import re
from collections import Counter

import psycopg2


# Legitimate commission types to preserve (after "la commission")
_LEGITIMATE_SUFFIXES = re.compile(
    r"^la\s+commission\s+"
    r"(spéciale|spécial|speciale|spéclale|spéciate|spéciaje|spéciate|spéclate|apéciale|spécia\w*"
    r"|mixte\b.*"
    r"|nationale\b.*"
    r"|départementale\b.*|départemental\b.*"
    r"|communale\b.*"
    r"|consultative\b.*"
    r"|locale\b.*"
    r"|municipale\b.*"
    r"|fiscale\b.*"
    r"|rogatoire\b.*"
    r"|économique\b.*"
    r"|exécutive\b.*"
    r"|parita\w*"
    r")$",
    re.IGNORECASE,
)

# OCR variants of "spéciale"
_SPECIALE_TYPOS = re.compile(
    r"^la\s+commission\s+"
    r"(spéclale|spéciate|spéciaje|spéclate|apéciale|spécia\w*|speciale|spécial)$",
    re.IGNORECASE,
)


def normalize_commission_author(auteur: str) -> str:
    """Normalize commission author variants to canonical form."""
    # Normalize whitespace (newlines, multiple spaces)
    normalized = re.sub(r"\s+", " ", auteur).strip()

    # Exact match "la commission" or "la Commission"
    if re.match(r"^la\s+commission$", normalized, re.IGNORECASE):
        return "la commission"

    # Check for legitimate suffixes
    if _LEGITIMATE_SUFFIXES.match(normalized):
        # Fix OCR typos of "spéciale"
        if _SPECIALE_TYPOS.match(normalized):
            return "la commission spéciale"
        # Fix "mixt..." truncations
        if re.match(r"^la\s+commission\s+mixt\w*$", normalized, re.IGNORECASE):
            return "la commission mixte"
        # Keep as-is (legitimate form)
        return normalized

    # Everything else: truncation or text leak → normalize to "la commission"
    return "la commission"


def main():
    parser = argparse.ArgumentParser(description="Normalize commission author variants")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    parser.add_argument("--db", default="postgresql://localhost/legislateur_local", help="DB URL")
    parser.add_argument("--batch", type=int, default=5000, help="Batch size for updates")
    args = parser.parse_args()

    conn = psycopg2.connect(args.db)
    cur = conn.cursor()

    # --- Phase 1: Analyze ---
    print("=" * 70)
    print("PHASE 1: ANALYSE - Variantes 'la commission'")
    print("=" * 70)

    cur.execute("""
        SELECT id, auteur FROM amendements
        WHERE auteur ILIKE 'la commission%' OR auteur ILIKE 'commission%'
    """)
    rows = cur.fetchall()
    print(f"Total candidats: {len(rows):,}")

    changes = []
    unchanged = 0
    variant_mapping = {}  # old → new for display

    for row_id, old_auteur in rows:
        new_auteur = normalize_commission_author(old_auteur)
        if new_auteur != old_auteur:
            changes.append((row_id, old_auteur, new_auteur))
            if old_auteur not in variant_mapping:
                variant_mapping[old_auteur] = new_auteur
        else:
            unchanged += 1

    print(f"Inchangés: {unchanged:,}")
    print(f"À modifier: {len(changes):,}")

    # --- Phase 2: Show mapping ---
    print(f"\n{'=' * 70}")
    print(f"PHASE 2: MAPPING DES VARIANTES ({len(variant_mapping)} formes)")
    print("=" * 70)

    # Group by target
    by_target = {}
    for old, new in sorted(variant_mapping.items()):
        by_target.setdefault(new, []).append(old)

    for target, sources in sorted(by_target.items()):
        count = sum(1 for _, o, _ in changes if o in sources)
        print(f"\n  → \"{target}\" ({count:,} amendements)")
        for src in sorted(sources, key=lambda x: -sum(1 for _, o, _ in changes if o == x))[:15]:
            src_count = sum(1 for _, o, _ in changes if o == src)
            print(f"      [{src_count:4d}x] {repr(src)[:100]}")
        if len(sources) > 15:
            print(f"      ... et {len(sources) - 15} autres variantes")

    # --- Phase 3: Post-normalization distribution ---
    print(f"\n{'=' * 70}")
    print("PHASE 3: DISTRIBUTION POST-NORMALISATION")
    print("=" * 70)

    all_new = Counter()
    for _, old_auteur in rows:
        new = normalize_commission_author(old_auteur)
        all_new[new] += 1

    for auteur, count in all_new.most_common():
        print(f"  x{count:6d}: {auteur}")

    # --- Phase 4: Verifications ---
    print(f"\n{'=' * 70}")
    print("PHASE 4: VÉRIFICATIONS")
    print("=" * 70)

    issues = []

    # Check no auteur becomes empty
    empty = [c for c in changes if not c[2] or not c[2].strip()]
    if empty:
        issues.append(f"! {len(empty)} auteurs deviendraient vides")

    # Check no newlines remain
    with_newlines = [c for c in changes if "\n" in c[2]]
    if with_newlines:
        issues.append(f"! {len(with_newlines)} auteurs contiennent encore des retours à la ligne")

    # Check legitimate forms preserved
    legit_forms = ["la commission spéciale", "la commission mixte",
                   "la commission départementale", "la commission nationale"]
    for form in legit_forms:
        count_before = sum(1 for _, old, _ in changes if form in old.lower().replace("\n", " "))
        count_after = sum(1 for _, _, new in changes if form in new.lower())
        if count_before > 0:
            print(f"  \"{form}\": {count_before} avant → {count_after} après (préservés)")

    if not issues:
        print("  Aucun problème détecté")
    else:
        for issue in issues:
            print(f"  {issue}")

    # --- Phase 5: Apply or not ---
    if not args.apply:
        print(f"\n{'=' * 70}")
        print("MODE DRY RUN")
        print(f"Relancer avec --apply pour appliquer les {len(changes):,} modifications")
        print("=" * 70)
        conn.close()
        return

    if not changes:
        print("\nAucune modification nécessaire.")
        conn.close()
        return

    print(f"\n{'=' * 70}")
    print(f"PHASE 5: APPLICATION ({len(changes):,} modifications)")
    print("=" * 70)

    applied = 0
    for i in range(0, len(changes), args.batch):
        batch = changes[i:i + args.batch]
        for row_id, _, new_auteur in batch:
            cur.execute("UPDATE amendements SET auteur = %s WHERE id = %s", (new_auteur, row_id))
        conn.commit()
        applied += len(batch)
        print(f"  Batch {i // args.batch + 1}: {applied:,} / {len(changes):,}")

    print(f"\n  {applied:,} amendements mis à jour")

    # --- Phase 6: Post-apply verification ---
    print(f"\n{'=' * 70}")
    print("PHASE 6: VÉRIFICATION POST-APPLICATION")
    print("=" * 70)

    cur.execute("""
        SELECT auteur, COUNT(*) FROM amendements
        WHERE auteur ILIKE 'la commission%'
        GROUP BY auteur ORDER BY COUNT(*) DESC
    """)
    print("Distribution auteur 'la commission' post-migration:")
    for auteur, count in cur.fetchall():
        print(f"  x{count:6d}: {repr(auteur)}")

    cur.execute("SELECT COUNT(*) FROM amendements WHERE auteur LIKE E'%\\n%'")
    print(f"\nAuteurs avec retour à la ligne: {cur.fetchone()[0]}")

    conn.close()
    print("\n  Migration terminée")


if __name__ == "__main__":
    main()
