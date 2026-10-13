#!/usr/bin/env python3
"""Fix data quality issues in article_modifications.json.

Fixes:
1. Normalize chambre: "SENAT" → "Sénat" (2,139 quotes)
2. Fill missing chambre fields where inferrable from orateur/fonction (139 quotes)
3. Deduplicate orateur names ("M. X. M. X" → "M. X")
4. Replace unresolved PA references with "Inconnu"
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

INDEX_PATH = Path("data/article_modifications.json")


def fix_chambre(quote: dict) -> dict:
    """Normalize chambre field."""
    chambre = quote.get("chambre", "")

    # Normalize SENAT → Sénat
    if chambre == "SENAT":
        quote["chambre"] = "Sénat"
        return quote

    # Fill missing chambre by inferring from fonction/orateur
    if not chambre:
        fonction = (quote.get("fonction") or "").lower()
        orateur = (quote.get("orateur") or "").lower()

        if any(kw in fonction for kw in ("sénateur", "senateur", "sénat")):
            quote["chambre"] = "Sénat"
        elif any(kw in fonction for kw in ("député", "depute", "assemblée")):
            quote["chambre"] = "AN"
        elif "rapporteur" in fonction:
            # Could be either chamber — check orateur for hints
            if "sénat" in orateur:
                quote["chambre"] = "Sénat"
            else:
                quote["chambre"] = "AN"  # default to AN for rapporteurs
        elif any(kw in fonction for kw in ("ministre", "secrétaire")):
            quote["chambre"] = "AN"  # ministers typically speak at AN
        else:
            # Can't infer, leave as empty but add the key
            quote["chambre"] = ""

    return quote


def fix_orateur(quote: dict) -> dict:
    """Fix orateur field: dedup and resolve PA references."""
    orateur = quote.get("orateur", "")

    if not orateur:
        return quote

    # Fix unresolved PA references (e.g., "PA774958")
    if re.match(r'^PA\d+$', orateur):
        quote["orateur"] = "Inconnu"
        return quote

    # Fix duplicated names: "M. Jacques Mézard. M. Jacques Mézard"
    # Split on ". " and check for duplicates
    parts = orateur.split(". ")
    if len(parts) == 2 and parts[0] == parts[1]:
        quote["orateur"] = parts[0]
    elif len(parts) > 2:
        # More complex dedup
        seen = set()
        unique_parts = []
        for p in parts:
            p_clean = p.strip()
            if p_clean and p_clean not in seen:
                seen.add(p_clean)
                unique_parts.append(p_clean)
        quote["orateur"] = ". ".join(unique_parts)

    return quote


def main():
    if not INDEX_PATH.exists():
        print(f"ERROR: {INDEX_PATH} not found")
        sys.exit(1)

    with open(INDEX_PATH) as f:
        data = json.load(f)

    articles = data.get("articles", {})

    stats = {
        "senat_normalized": 0,
        "chambre_filled": 0,
        "chambre_unfillable": 0,
        "orateur_deduped": 0,
        "pa_resolved": 0,
        "total_quotes_processed": 0,
    }

    for article_ref, art_data in articles.items():
        curated = art_data.get("curated_quotes", {})
        if not curated:
            continue

        for year, quotes in curated.items():
            if not isinstance(quotes, list):
                continue

            for quote in quotes:
                stats["total_quotes_processed"] += 1
                old_chambre = quote.get("chambre", "")
                old_orateur = quote.get("orateur", "")

                quote = fix_chambre(quote)
                quote = fix_orateur(quote)

                new_chambre = quote.get("chambre", "")
                new_orateur = quote.get("orateur", "")

                if old_chambre == "SENAT" and new_chambre == "Sénat":
                    stats["senat_normalized"] += 1
                elif not old_chambre and new_chambre:
                    stats["chambre_filled"] += 1
                elif not old_chambre and not new_chambre:
                    stats["chambre_unfillable"] += 1

                if old_orateur != new_orateur:
                    if re.match(r'^PA\d+$', old_orateur):
                        stats["pa_resolved"] += 1
                    else:
                        stats["orateur_deduped"] += 1

    # Write back
    with open(INDEX_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Fixed article_modifications.json:")
    print(f"  Quotes processed: {stats['total_quotes_processed']}")
    print(f"  SENAT → Sénat: {stats['senat_normalized']}")
    print(f"  Missing chambre filled: {stats['chambre_filled']}")
    print(f"  Missing chambre unfillable: {stats['chambre_unfillable']}")
    print(f"  PA references resolved: {stats['pa_resolved']}")
    print(f"  Orateur deduplicated: {stats['orateur_deduped']}")


if __name__ == "__main__":
    main()
