#!/usr/bin/env python3
"""Migration script: clean newlines and procedural text from amendment auteurs.

1,372 amendments have \n in the auteur field from three sources:
A. "la\ncommission..." variants (~710)
B. Names split by \n: "M. Charles\nLederman" (~200)
C. Procedural text leak: "M. Brottes\nest défendu" (~460)

Usage:
    python scripts/migrate_clean_auteur_newlines.py              # Dry run
    python scripts/migrate_clean_auteur_newlines.py --apply       # Apply
"""

import argparse
import re
from collections import Counter

import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from migrate_clean_auteur_commission import normalize_commission_author
from src.parsers.amendments import normalize_amendment_author


# Procedural keywords that signal where the actual name ends
# and session commentary begins
_PROCEDURAL_PATTERNS = re.compile(
    r"\s+(?:"
    # "est ..." patterns
    r"est retir��|est retir\b|est d��fendu|est maintenu|est devenu|est donc"
    r"|est ��galement|est identique|est sans doute|est incompatible"
    r"|est originale|est mieux|est réservé|est-il|est devenue?"
    r"|est soutenu|est adopté|est repris|est ineilleur"
    r"|est complété|est satisfait"
    # "sont ..." patterns
    r"|sont devenus?|sont réservés|sont bonnes|sont pas|sont partiellement"
    # "et ..." patterns (co-authors, cross-references)
    r"|et amendements? identiques?|et sous-amendements?"
    r"|et les (?:membres|commissaires|amendements)"
    r"|et des (?:membres|auteurs)"
    r"|et plusieurs de ses|et plusleurs de"
    r"|et de (?:ses|mes|moi|Mme|certains|la com)"
    r"|et du (?:groupe|sous-amendement|dép��t)"
    r"|et il est|et le sous-amendement|et votre"
    # "ne ..." patterns
    r"|ne sont pas|ne nous|ne me|ne peut|ne permet"
    # verb forms
    r"|devient|deviennent|tombe"
    r"|sera appelé|sera satisfait|serait"
    # "qui ..." patterns
    r"|qui propose|qui tend|qui porte|qui renvoie|qui souhaite|qui justifie"
    r"|qui reprend|qui avait|qui demande|qui me paraît"
    # gerunds/participles
    r"|tendant|visant|portant|allant|insérant"
    r"|me parai[tî]|nous semble|me laisse"
    r"|étant|étail|était|ayant|aurait|pourrait|pouvait"
    r"|semblait|semble"
    # past forms
    r"|ont été|a pour objet|a été|avait été"
    # misc procedural
    r"|pose un|posant un"
    r"|introduit|constitue|impliquait"
    r"|commence de|exprimait|reprend|revêt"
    r"|le rapporteur|le ministre|la commission|la rapporteur"
    r"|rapporteur |rapporteur$|ministre$|commission$"
    r"|par les mots|par le|par un"
    r"|puisque|puisqu|parce qu|car |mais |même si"
    r"|se lirait|se trouve|se substitue|substitue la"
    r"|je ne|je vous|je retire|je suis"
    r"|lorsque|pourquoi|comment"
    r"|avant P|avant le|avant la"
    r"|propose la|porte une|marque une"
    r"|de façon|de bien|de ne"
    r"|du groupe (?:communiste|socialiste|des)"
    r"|des collègues|des collégues"
    # Extra patterns from analysis
    r"|ont exactement|ne supprime"
    r"|au patriotisme|et celle qui|et la position"
    r"|et avait émis|tient compte"
    r"|que les garanties|afin de prévoir"
    r"|avec le sous|renforcent|sur le fait"
    r"|est ainsi (?:rédigé|libellé)"
    r"|qui aient été|procède du"
    r")\b",
    re.IGNORECASE,
)


def clean_auteur_newlines(auteur: str) -> str:
    """Clean newlines and procedural text from auteur field."""
    # Replace newlines with spaces
    cleaned = re.sub(r"\n+", " ", auteur).strip()

    # Category A: commission variants
    if re.match(r"^la\s+commission\b", cleaned, re.IGNORECASE):
        return normalize_commission_author(cleaned)

    # Category C: strip procedural text (before normalization)
    proc_match = _PROCEDURAL_PATTERNS.search(cleaned)
    if proc_match:
        cleaned = cleaned[:proc_match.start()].strip()

    # Strip co-authors without comma: "M. Dupont Mme Durand" → "M. Dupont"
    coauthor_match = re.search(r"\s+(?:Mme|Mmes|Mlle|MM\.)\s+[A-ZÀ-Ú]", cleaned)
    if coauthor_match and not cleaned[:coauthor_match.start()].rstrip().endswith(","):
        cleaned = cleaned[:coauthor_match.start()].strip()

    # Strip trailing procedural text: "... précédemment réservé", "... avant Particle"
    cleaned = re.sub(r"\s+(?:précédemment|avant\s+[Pp]).*$", "", cleaned)
    # Strip "est retiré/adopté/etc" that wasn't caught by _PROCEDURAL_PATTERNS
    cleaned = re.sub(r"\s+est\s+(?:retir|adopt|maintenu|soutenu|défendu|repris|devenu)\w*\b.*$", "", cleaned, flags=re.IGNORECASE)

    # Category B + general: normalize the name
    if cleaned:
        result = normalize_amendment_author(cleaned)
        # If result is just a title without a name, or a function reference, nullify
        if result and (
            result.rstrip(".") in ("M", "Mme", "Mlle", "MM")
            or re.match(r"^(?:M\.|Mme|Mlle)\s+(?:la\s+)?(?:le\s+)?(?:rapporteur|ministre|président)", result, re.IGNORECASE)
        ):
            return None
        return result if result else cleaned

    return auteur.replace("\n", " ").strip()


def main():
    parser = argparse.ArgumentParser(description="Clean newlines from amendment auteurs")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    parser.add_argument("--db", default="postgresql://localhost/legislateur_local", help="DB URL")
    parser.add_argument("--batch", type=int, default=5000, help="Batch size")
    parser.add_argument("--show-samples", type=int, default=40, help="Samples to show")
    args = parser.parse_args()

    conn = psycopg2.connect(args.db)
    cur = conn.cursor()

    # --- Phase 1: Analyze ---
    print("=" * 70)
    print("PHASE 1: ANALYSE - Auteurs avec retours à la ligne")
    print("=" * 70)

    cur.execute(r"SELECT id, auteur FROM amendements WHERE auteur LIKE E'%\n%'")
    rows = cur.fetchall()
    print(f"Total: {len(rows):,}")

    changes = []
    unchanged = 0
    categories = Counter()

    for row_id, old_auteur in rows:
        new_auteur = clean_auteur_newlines(old_auteur)
        if new_auteur != old_auteur:
            changes.append((row_id, old_auteur, new_auteur))
            # Classify
            if re.match(r"^la\s", old_auteur.replace("\n", " "), re.IGNORECASE):
                categories["A: la commission"] += 1
            elif _PROCEDURAL_PATTERNS.search(old_auteur.replace("\n", " ")):
                categories["C: texte procédural"] += 1
            else:
                categories["B: nom coupé"] += 1
        else:
            unchanged += 1

    print(f"Inchangés: {unchanged}")
    print(f"À modifier: {len(changes):,}")
    print(f"\nCatégories:")
    for cat, count in categories.most_common():
        print(f"  {cat}: {count:,}")

    # --- Phase 2: Show samples ---
    print(f"\n{'=' * 70}")
    print(f"PHASE 2: ÉCHANTILLONS ({min(args.show_samples, len(changes))} sur {len(changes):,})")
    print("=" * 70)

    # Show diverse samples by category
    cat_a = [(r, o, n) for r, o, n in changes if re.match(r"^la\s", o.replace("\n", " "), re.IGNORECASE)]
    cat_c = [(r, o, n) for r, o, n in changes if not re.match(r"^la\s", o.replace("\n", " "), re.IGNORECASE) and _PROCEDURAL_PATTERNS.search(o.replace("\n", " "))]
    cat_b = [(r, o, n) for r, o, n in changes if (r, o, n) not in cat_a and (r, o, n) not in cat_c]

    for label, items in [("A: la commission", cat_a), ("B: nom coupé", cat_b), ("C: texte procédural", cat_c)]:
        count_to_show = min(args.show_samples // 3, len(items))
        if count_to_show == 0:
            continue
        print(f"\n  --- {label} ({len(items)} total) ---")
        for row_id, old, new in items[:count_to_show]:
            print(f"  id={row_id}")
            print(f"    AVANT: {repr(old)[:100]}")
            print(f"    APRÈS: {(new or 'NULL')[:80]}")

    # --- Phase 3: Verifications ---
    print(f"\n{'=' * 70}")
    print("PHASE 3: VÉRIFICATIONS")
    print("=" * 70)

    issues = []

    # Check no auteur becomes empty
    empty = [c for c in changes if not c[2] or not c[2].strip()]
    if empty:
        issues.append(f"! {len(empty)} auteurs deviendraient vides")
        for row_id, old, new in empty[:5]:
            print(f"    id={row_id}: {repr(old)[:80]} → {repr(new)}")

    # Check no newlines remain
    with_nl = [c for c in changes if c[2] and "\n" in c[2]]
    if with_nl:
        issues.append(f"! {len(with_nl)} auteurs contiennent encore des \\n")
        for row_id, old, new in with_nl[:5]:
            print(f"    id={row_id}: {repr(new)[:80]}")

    # Post-normalization length distribution
    new_lengths = [len(c[2]) for c in changes if c[2]]
    if new_lengths:
        print(f"\nLongueur après nettoyage: min={min(new_lengths)}, max={max(new_lengths)}, avg={sum(new_lengths)/len(new_lengths):.1f}")
        very_long = [(r, o, n) for r, o, n in changes if n and len(n) > 100]
        if very_long:
            print(f"  > 100 chars: {len(very_long)} cas")
            for row_id, old, new in very_long[:5]:
                print(f"    id={row_id}: {new[:120]}")

    # Distribution of new values
    nullified = sum(1 for _, _, n in changes if n is None)
    if nullified:
        print(f"\n  → {nullified} auteurs seront nullifiés (titre sans nom)")

    new_values = Counter(n for _, _, n in changes if n)
    print(f"\nTop 15 auteurs après nettoyage:")
    for val, count in new_values.most_common(15):
        print(f"  x{count:4d}: {val[:80]}")

    if not issues:
        print("\n  Aucun problème détecté")
    else:
        for issue in issues:
            print(f"  {issue}")

    # --- Phase 4: Apply or not ---
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
    print(f"PHASE 4: APPLICATION ({len(changes):,} modifications)")
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

    # --- Phase 5: Post-apply verification ---
    print(f"\n{'=' * 70}")
    print("PHASE 5: VÉRIFICATION POST-APPLICATION")
    print("=" * 70)

    cur.execute(r"SELECT COUNT(*) FROM amendements WHERE auteur LIKE E'%\n%'")
    print(f"  Auteurs avec \\n restants: {cur.fetchone()[0]}")

    cur.execute("""
        SELECT auteur, COUNT(*) FROM amendements
        WHERE auteur ILIKE 'la commission%'
        GROUP BY auteur ORDER BY COUNT(*) DESC LIMIT 10
    """)
    print("\nDistribution 'la commission' post-migration:")
    for auteur, count in cur.fetchall():
        print(f"  x{count:6d}: {auteur}")

    conn.close()
    print("\n  Migration terminée")


if __name__ == "__main__":
    main()
