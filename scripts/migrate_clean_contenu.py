#!/usr/bin/env python3
"""Migration script: remove leaked function prefix from intervention contenu.

When the AN HTML parser fallback is triggered, the speaker's function/role text
(from the <i> tag) can remain at the beginning of the contenu field. This script
detects and removes these leaked prefixes heuristically.

Usage:
    python scripts/migrate_clean_contenu.py                  # Dry run (default)
    python scripts/migrate_clean_contenu.py --apply           # Apply changes
    python scripts/migrate_clean_contenu.py --apply --batch=10000
"""

import argparse
import re
import sys
from collections import Counter

import psycopg2


# ── Detection patterns ──────────────────────────────────────────────────────

# Function keywords that should never start actual speech content.
# These are the known prefixes leaked from HTML italic tags.
_FUNCTION_KEYWORDS = [
    "rapporteur",
    "ministre",
    "secrétaire",
    "président",
    "présidente",
    "commissaire",
    "garde des sceaux",
]

# Extended function suffixes that may appear after the keyword
# e.g. "rapporteur général de la commission des finances, de l'économie..."
# e.g. "rapporteur spécial." or "ministre délégué."
_FUNCTION_CONTINUATION = re.compile(
    r"^("
    # Optional "e" suffix for feminine (rapporteure)
    r"e?"
    # Optional qualifiers - all the words that can follow a function keyword
    # rapporteur: général(e), spécial(e), pour avis, au nom, suppléant(e), thématique
    # ministre: délégué(e), adjoint(e)
    # secrétaire: d'État, d'Etat, général(e)
    # président: de séance, adjoint(e)
    r"(?:\s+(?:"
    r"général(?:e)?|spécial(?:e)?|délégué(?:e)?|adjoint(?:e)?"
    r"|suppléant(?:e)?|thématique"
    r"|pour\s+avis|au\s+nom"
    r"|d[''e]\s*[EÉée]tat"
    r"|de\s+séance"
    r"))*"
    # Optional "auprès du ministre de..." clause
    r"(?:\s+auprès\s+du\s+ministre\s+[^.!?]*?)?"
    # Optional commission name: "de la commission des..."
    r"(?:\s+de\s+(?:la\s+commission|l'Assemblée)\s+[^.!?]*?)?"
    # Optional extra qualifiers after commission: "pour la branche vieillesse"
    r"(?:\s+(?:pour|saisie?\s+pour|chargée?\s+de)\s+[^.!?]*?)?"
    r")"
    # Must end with punctuation, whitespace, or newline
    r"[\n.,;:\s]",
    re.IGNORECASE | re.DOTALL,
)

# For "général" suffix leak (when "rapporteur" was already stripped but "général" remains)
_GENERAL_SUFFIX = re.compile(
    r"^général(?:e)?"
    r"(?:\s+de\s+la\s+commission\s+[^.!?]*?)?"
    r"[\n.,;:\s]",
    re.IGNORECASE | re.DOTALL,
)

# For "de la commission ..." suffix leak
_COMMISSION_SUFFIX = re.compile(
    r"^de\s+la\s+commission\s+[^.!?\n]*?"
    r"[\n.,;:\s]",
    re.IGNORECASE | re.DOTALL,
)

# For "pour avis..." suffix leak
_POUR_AVIS_SUFFIX = re.compile(
    r"^pour\s+avis\s+[^.!?\n]*?"
    r"[\n.,;:\s]",
    re.IGNORECASE | re.DOTALL,
)

# For "délégué(e)" suffix leak (from "ministre délégué" where "ministre" was gone)
_DELEGUE_SUFFIX = re.compile(
    r"^délégué(?:e)?"
    r"(?:\s+auprès\s+[^.!?\n]*?)?"
    r"(?:\s+chargée?\s+[^.!?\n]*?)?"
    r"[\n.,;:\s]",
    re.IGNORECASE | re.DOTALL,
)

# For "spécial(e)" suffix leak (from "rapporteur spécial")
_SPECIAL_SUFFIX = re.compile(
    r"^spécial(?:e)?"
    r"(?:\s+de\s+la\s+commission\s+[^.!?\n]*?)?"
    r"[\n.,;:\s]",
    re.IGNORECASE | re.DOTALL,
)


def _find_speech_start(text: str) -> int | None:
    """Find where actual speech begins after function text.

    Looks for a sentence-ending pattern (. or newline) followed by an uppercase
    letter, which signals the start of the actual speech.
    Returns the index of the first uppercase letter, or None if not found.
    """
    # Pattern: punctuation/whitespace + uppercase letter = speech start
    for i in range(len(text) - 1):
        ch = text[i]
        if ch in '.\n':
            # Find next non-whitespace/punctuation character
            rest = text[i + 1:].lstrip(".,;: \xa0\n")
            if rest and rest[0].isupper():
                return len(text) - len(rest)
            # Also handle no-space case: "rapporteur.Cela" → "C" right after "."
            if i + 1 < len(text) and text[i + 1].isupper():
                return i + 1
    return None


def strip_function_prefix(contenu: str, fonction: str | None) -> str | None:
    """Remove leaked function prefix from the start of contenu.

    Strategy: if contenu starts with a known function keyword, find the first
    sentence boundary (. or newline followed by uppercase) and strip everything
    before it. This handles all suffix combinations without needing to enumerate
    every possible function title.

    Returns the cleaned contenu, or None if no change needed.
    """
    if not contenu:
        return None

    original = contenu
    contenu_lower = contenu.lower()

    # Check if contenu starts with any known function keyword
    matched_keyword = None
    for keyword in _FUNCTION_KEYWORDS:
        if contenu_lower.startswith(keyword):
            matched_keyword = keyword
            break

    # Also check suffix patterns (partial leak)
    suffix_keywords = ["général", "spécial", "délégué", "de la commission", "pour avis"]
    if not matched_keyword:
        for kw in suffix_keywords:
            if contenu_lower.startswith(kw):
                matched_keyword = kw
                break

    if not matched_keyword:
        return None

    # For suffix patterns, require that the fonction field confirms a relevant role
    if matched_keyword in suffix_keywords:
        if not fonction:
            return None
        fn_lower = fonction.lower()
        # "général/spécial" → must be rapporteur
        # "délégué" → must be ministre
        # "de la commission/pour avis" → must be rapporteur/ministre/president
        valid = False
        if matched_keyword in ("général", "spécial", "pour avis"):
            valid = "rapporteur" in fn_lower
        elif matched_keyword == "délégué":
            valid = "ministre" in fn_lower or "senateur" in fn_lower
        elif matched_keyword == "de la commission":
            valid = any(k in fn_lower for k in ("rapporteur", "ministre", "president", "senateur"))
        if not valid:
            return None

    # Find where the actual speech starts
    # Limit search to first 300 chars (function titles never exceed this)
    search_text = contenu[:300]
    speech_start = _find_speech_start(search_text)

    if speech_start is not None and speech_start > len(matched_keyword):
        cleaned = contenu[speech_start:]
        if cleaned and cleaned != original:
            return cleaned

    # Fallback: if keyword is immediately followed by ". " or ".\n" or just "."
    rest_after = contenu[len(matched_keyword):]
    stripped = rest_after.lstrip(".,;: \xa0\n")
    if stripped and stripped != original and stripped[0].isupper():
        return stripped

    return None


def main():
    parser = argparse.ArgumentParser(description="Clean leaked function prefix from intervention contenu")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    parser.add_argument("--db", default="postgresql://localhost/legislateur_local", help="DB URL")
    parser.add_argument("--batch", type=int, default=10000, help="Batch size for updates")
    parser.add_argument("--show-samples", type=int, default=30, help="Number of samples to show")
    args = parser.parse_args()

    conn = psycopg2.connect(args.db)
    cur = conn.cursor()

    # --- Phase 1: Analyze scope ---
    print("=" * 70)
    print("PHASE 1: ANALYSE - Détection des interventions affectées")
    print("=" * 70)

    cur.execute("SELECT COUNT(*) FROM interventions")
    total = cur.fetchone()[0]
    print(f"Total interventions: {total:,}")

    # Build SQL WHERE clause for candidate rows (fast pre-filter)
    where_clause = " OR ".join(
        f"contenu ILIKE '{kw}%'" for kw in _FUNCTION_KEYWORDS
    )
    where_clause += " OR contenu ILIKE 'général%'"
    where_clause += " OR contenu ILIKE 'de la commission%'"
    where_clause += " OR contenu ILIKE 'pour avis%'"

    cur.execute(f"SELECT COUNT(*) FROM interventions WHERE {where_clause}")
    candidates = cur.fetchone()[0]
    print(f"Candidats (pré-filtre SQL): {candidates:,}")

    # Fetch candidates
    print("Chargement des candidats...")
    cur.execute(f"""
        SELECT id, orateur, fonction, contenu
        FROM interventions
        WHERE {where_clause}
    """)
    rows = cur.fetchall()
    print(f"Chargés: {len(rows):,} lignes")

    # Apply heuristic
    changes = []  # (id, old_start, new_contenu)
    stats = Counter()
    false_positives = []

    for row_id, orateur, fonction, contenu in rows:
        new_contenu = strip_function_prefix(contenu, fonction)
        if new_contenu is not None:
            changes.append((row_id, contenu[:80], new_contenu, orateur, fonction))
            # Classify
            contenu_lower = contenu.lower()
            if contenu_lower.startswith("rapporteur"):
                stats["rapporteur"] += 1
            elif contenu_lower.startswith("ministre"):
                stats["ministre"] += 1
            elif contenu_lower.startswith("secrétaire"):
                stats["secrétaire"] += 1
            elif contenu_lower.startswith("président"):
                stats["président"] += 1
            elif contenu_lower.startswith("général"):
                stats["général (suffix)"] += 1
            elif contenu_lower.startswith("de la commission"):
                stats["de la commission (suffix)"] += 1
            elif contenu_lower.startswith("pour avis"):
                stats["pour avis (suffix)"] += 1
            else:
                stats["other"] += 1

    print(f"\nRésultats:")
    print(f"  Candidats SQL:  {len(rows):,}")
    print(f"  À modifier:     {len(changes):,}")
    print(f"  Faux positifs:  {len(rows) - len(changes):,}")
    print(f"\nDétail par type:")
    for category, count in stats.most_common():
        print(f"  {category}: {count:,}")

    # --- Phase 2: Show samples ---
    print(f"\n{'=' * 70}")
    print(f"PHASE 2: ÉCHANTILLONS ({min(args.show_samples, len(changes))} sur {len(changes):,})")
    print("=" * 70)

    # Diverse samples
    import random
    if changes:
        # Group by type
        sample_indices = set()
        # First few of each type
        by_type = {}
        for i, (row_id, old_start, new, orateur, fn) in enumerate(changes):
            key = old_start[:15].lower().split('.')[0].split(',')[0].strip()
            if key not in by_type:
                by_type[key] = []
            by_type[key].append(i)

        for key, indices in by_type.items():
            for idx in indices[:3]:
                sample_indices.add(idx)
            if len(sample_indices) >= args.show_samples:
                break

        # Fill with random
        while len(sample_indices) < min(args.show_samples, len(changes)):
            sample_indices.add(random.randint(0, len(changes) - 1))

        for idx in sorted(sample_indices)[:args.show_samples]:
            row_id, old_start, new, orateur, fn = changes[idx]
            print(f"\n--- id={row_id} | {orateur[:25]} | fn={fn} ---")
            print(f"  AVANT: {old_start}")
            print(f"  APRÈS: {new[:80]}")

    # --- Phase 3: Verification ---
    print(f"\n{'=' * 70}")
    print("PHASE 3: VÉRIFICATIONS")
    print("=" * 70)

    issues = []

    # 1. Check no contenu becomes empty
    empty_after = [(c[0], c[1]) for c in changes if not c[2] or not c[2].strip()]
    if empty_after:
        issues.append(f"⚠ {len(empty_after)} contenu deviendraient vides après nettoyage")
        for row_id, old_start in empty_after[:5]:
            print(f"    id={row_id}: '{old_start}'")

    # 2. Check that cleaned content looks like speech (starts with uppercase or common patterns)
    bad_starts = []
    for row_id, old_start, new, orateur, fn in changes:
        first_char = new[0] if new else ""
        if first_char and not first_char.isupper() and first_char not in ('"', '«', "'", "(", "[", "–", "-"):
            # Lowercase start might be mid-sentence - potential over-stripping
            if not new.startswith(("l'", "d'", "n'", "s'", "j'", "qu'")):
                bad_starts.append((row_id, old_start, new[:60]))

    if bad_starts:
        issues.append(f"⚠ {len(bad_starts)} contenu nettoyés commencent par une minuscule (possible sur-nettoyage)")
        for row_id, old, new in bad_starts[:10]:
            print(f"    id={row_id}: AVANT='{old[:60]}' → APRÈS='{new}'")

    # 3. Check that function keywords no longer appear at start
    still_has_prefix = 0
    for _, _, new, _, _ in changes:
        if new and any(new.lower().startswith(kw) for kw in _FUNCTION_KEYWORDS):
            still_has_prefix += 1
    if still_has_prefix:
        issues.append(f"⚠ {still_has_prefix} contenu ont encore un préfixe de fonction après nettoyage")

    # 4. Check contenu length distribution (should not be drastically shorter)
    len_diffs = []
    for _, old_start, new, _, _ in changes:
        # old_start is truncated at 80 chars, so we can't compare directly
        # Just check that new is reasonably long
        if len(new) < 5:
            len_diffs.append(new)
    if len_diffs:
        issues.append(f"⚠ {len(len_diffs)} contenu nettoyés font moins de 5 caractères")

    if not issues:
        print("✓ Aucun problème détecté")
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

    applied = 0
    for i in range(0, len(changes), args.batch):
        batch = changes[i:i + args.batch]
        for row_id, _, new_contenu, _, _ in batch:
            cur.execute(
                "UPDATE interventions SET contenu = %s WHERE id = %s",
                (new_contenu, row_id)
            )
        conn.commit()
        applied += len(batch)
        pct = applied / len(changes) * 100
        print(f"  Batch {i // args.batch + 1}: {applied:,} / {len(changes):,} ({pct:.1f}%)")

    print(f"\n✓ {applied:,} interventions mises à jour")

    # --- Phase 5: Post-apply verification ---
    print(f"\n{'=' * 70}")
    print("PHASE 5: VÉRIFICATION POST-APPLICATION")
    print("=" * 70)

    for kw in _FUNCTION_KEYWORDS + ["général", "de la commission"]:
        cur.execute(f"SELECT COUNT(*) FROM interventions WHERE contenu ILIKE '{kw}%'")
        remaining = cur.fetchone()[0]
        print(f"  {kw:25s} restants: {remaining:,}")

    conn.close()
    print("\n✓ Migration terminée")


if __name__ == "__main__":
    main()
