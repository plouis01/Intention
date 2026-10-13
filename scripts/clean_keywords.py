"""Clean and deduplicate keywords in modifications_cgi_enriched.json.

Removes pure noise keywords and deduplicates near-identical entries.

Usage:
    python scripts/clean_keywords.py                # Dry run (show diff for sample articles)
    python scripts/clean_keywords.py --stats         # Corpus-wide stats only
    python scripts/clean_keywords.py --apply         # Apply changes
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Structural CGI terms - zero search value
_STRUCTURAL = {
    "section", "sous", "sous section", "paragraphe", "chapitre",
    "dispositions", "dispositions générales", "titre",
}

# Prepositions that make bigrams meaningful (e.g., "crédit d'impôt")
_PREPOSITIONS = {"d'", "de", "du", "des", "l'", "à", "au", "aux", "en", "et", "sur", "par", "pour"}


def _is_pure_number(kw: str) -> bool:
    return kw.replace("-", "").replace(" ", "").isdigit()


def _is_percentage(kw: str) -> bool:
    return "%" in kw


def _is_structural(kw: str) -> bool:
    return kw.lower() in _STRUCTURAL


def _is_short_single(kw: str) -> bool:
    """Single word < 6 chars without digits."""
    if " " in kw or "-" in kw:
        return False
    if any(c.isdigit() for c in kw):
        return False
    return len(kw) < 6


# Known legal terms that look like fragments but are real
_LEGAL_TERMS_WHITELIST = {
    "pacte dutreil", "flat tax", "credit bail", "crédit bail",
    "fonds communs", "taux forfaitaire", "plus values",
    "revenu global", "base imposable", "assiette fiscale",
}


def _is_fragment_bigram(kw: str) -> bool:
    """Two short words without preposition - likely a title split artifact."""
    if kw.lower() in _LEGAL_TERMS_WHITELIST:
        return False
    words = kw.split()
    if len(words) != 2:
        return False
    # If either word is a preposition/article, it's a real phrase
    if any(w.lower() in _PREPOSITIONS for w in words):
        return False
    if "'" in kw:
        return False  # Contraction = real phrase
    # Both words < 6 chars each = almost certainly a fragment
    # (raised from 8 to reduce false positives like "pacte dutreil")
    return all(len(w) < 6 for w in words)


def _normalize_for_dedup(s: str) -> str:
    """Normalize for case/accent dedup."""
    return unicodedata.normalize("NFD", s.lower()).encode("ascii", "ignore").decode()


def clean_keywords(keywords: list[str], min_keep: int = 3) -> tuple[list[str], dict]:
    """Clean a single article's keyword list.

    Returns (cleaned_keywords, stats_dict).
    """
    stats = {
        "before": len(keywords),
        "removed_numbers": 0,
        "removed_pcts": 0,
        "removed_structural": 0,
        "removed_short": 0,
        "removed_fragments": 0,
        "removed_case_dups": 0,
        "removed_substring_dups": 0,
    }

    # Phase 1: Remove noise
    kept = []
    for kw in keywords:
        if _is_pure_number(kw):
            stats["removed_numbers"] += 1
        elif _is_percentage(kw):
            stats["removed_pcts"] += 1
        elif _is_structural(kw):
            stats["removed_structural"] += 1
        elif _is_short_single(kw):
            stats["removed_short"] += 1
        elif _is_fragment_bigram(kw):
            stats["removed_fragments"] += 1
        else:
            kept.append(kw)

    # Phase 2: Case/accent dedup (keep first occurrence)
    seen_norm = {}
    deduped = []
    for kw in kept:
        n = _normalize_for_dedup(kw)
        if n in seen_norm:
            stats["removed_case_dups"] += 1
        else:
            seen_norm[n] = kw
            deduped.append(kw)

    # Phase 3: Substring dedup (remove shorter if longer exists)
    # Only for keywords > 4 chars to avoid removing useful short forms
    to_remove = set()
    for i, kw1 in enumerate(deduped):
        if len(kw1) <= 4:
            continue
        for j, kw2 in enumerate(deduped):
            if i == j:
                continue
            if kw1 in kw2 and kw1 != kw2:
                to_remove.add(kw1)
                break

    final = [kw for kw in deduped if kw not in to_remove]
    stats["removed_substring_dups"] = len(to_remove)

    # Guard: never go below min_keep
    if len(final) < min_keep and len(keywords) >= min_keep:
        final = keywords[:min_keep]
        stats["guard_triggered"] = True

    stats["after"] = len(final)
    stats["total_removed"] = stats["before"] - stats["after"]
    return final, stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--stats", action="store_true", help="Show corpus stats only")
    parser.add_argument("--sample", nargs="*", default=None,
                        help="Article refs to show detail for")
    args = parser.parse_args()

    path = Path("data/modifications_cgi_enriched.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    articles = data.get("articles", {})

    # Default sample articles for dry run
    sample_refs = args.sample or [
        "150-0 B ter", "787 B", "39", "199 terdecies-0 A", "200 A",
    ]

    total_before = 0
    total_after = 0
    total_removed = 0
    articles_changed = 0
    all_stats = {
        "removed_numbers": 0, "removed_pcts": 0, "removed_structural": 0,
        "removed_short": 0, "removed_fragments": 0,
        "removed_case_dups": 0, "removed_substring_dups": 0,
    }

    for ref, art in articles.items():
        kws = art.get("keywords", [])
        cleaned, stats = clean_keywords(kws)

        total_before += stats["before"]
        total_after += stats["after"]
        total_removed += stats["total_removed"]
        for k in all_stats:
            all_stats[k] += stats.get(k, 0)
        if stats["total_removed"] > 0:
            articles_changed += 1

        # Show detail for sample articles
        if ref in sample_refs and not args.stats:
            removed = set(kws) - set(cleaned)
            added = set(cleaned) - set(kws)  # should be empty
            print(f"\n{'='*60}")
            print(f"{ref}: {stats['before']} → {stats['after']} keywords (-{stats['total_removed']})")
            print(f"  Numbers: -{stats['removed_numbers']}  Pcts: -{stats['removed_pcts']}  "
                  f"Structural: -{stats['removed_structural']}  Short: -{stats['removed_short']}  "
                  f"Fragments: -{stats['removed_fragments']}")
            print(f"  Case dups: -{stats['removed_case_dups']}  Substring dups: -{stats['removed_substring_dups']}")
            if removed:
                print(f"  REMOVED: {sorted(removed)}")
            if added:
                print(f"  ADDED(?): {sorted(added)}")
            print(f"  KEPT: {sorted(cleaned)}")

        if args.apply:
            art["keywords"] = cleaned

    # Print corpus stats
    print(f"\n{'='*60}")
    print(f"CORPUS STATS")
    print(f"{'='*60}")
    print(f"Articles: {len(articles)} ({articles_changed} changed)")
    print(f"Keywords: {total_before} → {total_after} (-{total_removed}, -{total_removed/total_before*100:.1f}%)")
    print(f"Avg per article: {total_before/len(articles):.1f} → {total_after/len(articles):.1f}")
    print(f"\nBreakdown:")
    for k, v in sorted(all_stats.items()):
        if v > 0:
            print(f"  {k:30s}: -{v}")

    if args.apply:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\nSaved to {path}")
    elif not args.stats:
        print(f"\nDRY RUN - use --apply to save changes")


if __name__ == "__main__":
    main()
