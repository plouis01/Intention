#!/usr/bin/env python3
"""Audit and classify modifications for a CGI article.

For each modification in the article_modifications index, checks whether it
actually modifies the target article or a neighboring article (e.g., "39 ter",
"39 decies") by inspecting the law article text from the database.

Usage:
    python scripts/audit_article_mods.py "39"
    python scripts/audit_article_mods.py "39" --remove-false-positives
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.models import Article, Loi, get_session
from src.indexing.search import _build_contextualized_patterns

INDEX_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "article_modifications.json"
)

# Latin ordinal suffixes used in CGI article numbering
ORDINALS = (
    "bis|ter|quater|quinquies|sexies|septies|octies|nonies|"
    "decies|undecies|duodecies|terdecies|quaterdecies|"
    "quindecies|sexdecies|septendecies|octodecies|novodecies|vicies"
)


def classify_modification(
    article_ref: str,
    mod: dict,
    article_text: str | None,
    ctx_patterns: list[re.Pattern],
) -> str:
    """Classify a modification as direct, neighbor, indirect, or false_positive.

    Returns one of:
      - "direct": modifies the target article itself
      - "neighbor": modifies a neighboring article (e.g., 39 ter, 39 decies)
      - "indirect": mentions the target article in context but modifies another
      - "false_positive": no relation to the target article
    """
    if not article_text:
        return "no_data"

    text = article_text

    # Build neighbor detection pattern for this specific ref
    ref_escaped = re.escape(article_ref.strip())
    neighbor_re = re.compile(
        rf"\barticle\s+{ref_escaped}\s+(?:{ORDINALS}|[A-Z](?:\s|$))",
        re.IGNORECASE,
    )

    has_exact = any(p.search(text) for p in ctx_patterns)
    has_neighbor = bool(neighbor_re.search(text))

    # Check if text mentions the target article with "du CGI" or "du code général"
    cgi_re = re.compile(
        rf"\b{ref_escaped}\s+du\s+(?:cgi|code\s+g[eé]n[eé]ral)",
        re.IGNORECASE,
    )
    has_cgi_ref = bool(cgi_re.search(text))

    if has_exact or has_cgi_ref:
        # Mentions the target article directly
        if has_neighbor and not has_exact:
            return "neighbor"
        return "direct"

    if has_neighbor:
        return "neighbor"

    # Check for bare article ref in text (substring match)
    if article_ref.lower() in text.lower():
        return "indirect"

    return "false_positive"


def audit_article(article_ref: str, remove_fp: bool = False) -> None:
    """Audit all modifications for an article and print classification report."""
    # Load index
    with open(INDEX_PATH) as f:
        index_data = json.load(f)

    articles = index_data.get("articles", {})
    if article_ref not in articles:
        print(f"Article '{article_ref}' not found in index.")
        print(f"Available articles: {', '.join(sorted(articles.keys())[:20])}...")
        return

    art_data = articles[article_ref]
    modifications = art_data.get("modifications", [])
    print(f"\n=== Audit: Article {article_ref} du {art_data.get('code', 'CGI')} ===")
    print(f"Title: {art_data.get('titre', 'N/A')}")
    print(f"Total modifications in index: {len(modifications)}\n")

    # Build patterns
    ctx_patterns = _build_contextualized_patterns(article_ref)

    # Query DB for article texts
    session = get_session()
    results = {"direct": [], "neighbor": [], "indirect": [], "false_positive": [], "no_data": []}

    for i, mod in enumerate(modifications):
        law_numero = mod.get("law_numero", "")
        article_in_law = mod.get("article_in_law", "")

        # Try to find the law article text in DB
        article_text = None
        loi = session.query(Loi).filter(Loi.numero == law_numero).first()
        if loi:
            # Normalize article number for DB lookup
            art_num = article_in_law.replace("Art. ", "").replace("art. ", "").strip()
            db_article = (
                session.query(Article)
                .filter(Article.loi_id == loi.id, Article.numero_article == art_num)
                .first()
            )
            if db_article:
                article_text = db_article.contenu_texte

        classification = classify_modification(
            article_ref, mod, article_text, ctx_patterns
        )
        results[classification].append((i, mod, article_text))

    # Print report
    for category in ["direct", "neighbor", "indirect", "false_positive", "no_data"]:
        items = results[category]
        icons = {
            "direct": "+", "neighbor": "~", "indirect": "?",
            "false_positive": "X", "no_data": "-",
        }
        print(f"\n[{icons[category]}] {category.upper()} ({len(items)} mods)")
        print("-" * 60)
        for idx, mod, text in items:
            law_ref = f"{mod.get('law_type', '')} {mod.get('law_year', '')}"
            art_in = mod.get("article_in_law", "")
            nature = mod.get("nature", "")
            numero = mod.get("law_numero", "")
            print(
                f"  #{idx:3d} | {law_ref:10s} | {art_in:10s}"
                f" | {nature:15s} | {numero}"
            )
            if text and category in ("neighbor", "indirect", "false_positive"):
                # Show relevant snippet
                snippet = text[:200].replace("\n", " ")
                print(f"        Text: {snippet}...")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print(f"  Direct:         {len(results['direct']):3d}")
    print(f"  Neighbor:       {len(results['neighbor']):3d}")
    print(f"  Indirect:       {len(results['indirect']):3d}")
    print(f"  False positive: {len(results['false_positive']):3d}")
    print(f"  No data:        {len(results['no_data']):3d}")
    print(f"  TOTAL:          {len(modifications):3d}")

    if remove_fp and (results["false_positive"] or results["neighbor"]):
        fp_indices = {idx for idx, _, _ in results["false_positive"]}
        neighbor_indices = {idx for idx, _, _ in results["neighbor"]}
        to_remove = fp_indices | neighbor_indices

        new_mods = [m for i, m in enumerate(modifications) if i not in to_remove]
        print(f"\nRemoving {len(to_remove)} modifications (false_positive + neighbor)...")
        print(f"Before: {len(modifications)} → After: {len(new_mods)}")

        art_data["modifications"] = new_mods
        with open(INDEX_PATH, "w") as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)
        print(f"Updated {INDEX_PATH}")

    session.close()


def main():
    parser = argparse.ArgumentParser(description="Audit article modifications for false positives")
    parser.add_argument("article_ref", help="CGI article reference (e.g., '39', '150-0 B ter')")
    parser.add_argument(
        "--remove-false-positives",
        action="store_true",
        help="Remove false_positive and neighbor entries from the index",
    )
    args = parser.parse_args()
    audit_article(args.article_ref, remove_fp=args.remove_false_positives)


if __name__ == "__main__":
    main()
