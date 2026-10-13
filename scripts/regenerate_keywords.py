"""Regenerate keywords for all articles in modifications_cgi_enriched.json.

Merges (add-only) keywords from:
1. generate_all_keywords() with curated expansions
2. extract_keywords_from_law_text() using texte_actuel

Usage:
    python scripts/regenerate_keywords.py              # Dry run
    python scripts/regenerate_keywords.py --apply       # Apply changes
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.analysis.keyword_generator import generate_all_keywords


def quality_filter(keywords: set[str]) -> set[str]:
    """Filter out noise from generated keywords."""
    filtered = set()
    for kw in keywords:
        if len(kw) > 50:
            continue
        if len(kw) < 4 and not any(c.isdigit() for c in kw):
            continue
        if "," in kw:
            continue
        filtered.add(kw)
    return filtered


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    parser.add_argument("--sample", type=int, default=0, help="Only process N articles (for testing)")
    args = parser.parse_args()

    path = Path("data/modifications_cgi_enriched.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    articles = data.get("articles", {})
    total_added = 0
    articles_changed = 0

    refs = list(articles.keys())
    if args.sample:
        refs = refs[: args.sample]

    for i, ref in enumerate(refs):
        art = articles[ref]
        current = set(art.get("keywords", []))
        titre = art.get("titre", "")
        texte = (art.get("texte_actuel") or "")[:3000]

        generated = set(
            generate_all_keywords(ref, titre=titre, law_text=texte, include_curated=True)
        )
        filtered = quality_filter(generated)

        # Merge: add-only
        added = filtered - current
        if added:
            total_added += len(added)
            articles_changed += 1
            if args.apply:
                art["keywords"] = sorted(current | filtered)

        if (i + 1) % 500 == 0:
            print(f"  Processed {i + 1}/{len(refs)} articles...")

    print(f"\nResults: {articles_changed}/{len(refs)} articles enriched, {total_added} keywords added total")
    print(f"Average per changed article: {total_added / articles_changed:.1f}" if articles_changed else "")

    if args.apply:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Saved to {path}")
    else:
        print("DRY RUN - use --apply to save changes")


if __name__ == "__main__":
    main()
