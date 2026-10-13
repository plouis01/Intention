#!/usr/bin/env python3
"""
Cross-validate and fix misplaced quotes in article_modifications.json.

A quote is misplaced if:
1. It explicitly mentions "article X du CGI" or "article X du code général des impôts"
2. It does NOT mention the article it's filed under anywhere in the citation text
3. The mentioned article X exists in the index

Actions:
- MOVE: quote's primary topic matches target article better than source
- DELETE: quote is about a PLF article number or procedural noise
- KEEP: false positive — quote legitimately discusses the filed article
"""

import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

DATA_PATH = Path(__file__).parent.parent / "data" / "article_modifications.json"

# Patterns to detect CGI article references
CGI_PATTERNS = [
    # "article X du code général des impôts"
    r"article\s+([\d]+(?:[- ]\d+)?(?:\s*[A-Za-z]+(?:\s+[A-Za-z]+)*)?)\s+du\s+code\s+g[ée]n[ée]ral\s+des\s+imp[oô]ts",
    # "article X du CGI"
    r"article\s+([\d]+(?:[- ]\d+)?(?:\s*[A-Za-z]+(?:\s+[A-Za-z]+)*)?)\s+du\s+CGI",
    # "l'article X du code général des impôts"
    r"l[''']article\s+([\d]+(?:[- ]\d+)?(?:\s*[A-Za-z]+(?:\s+[A-Za-z]+)*)?)\s+du\s+code\s+g[ée]n[ée]ral\s+des\s+imp[oô]ts",
    # "l'article X du CGI"
    r"l[''']article\s+([\d]+(?:[- ]\d+)?(?:\s*[A-Za-z]+(?:\s+[A-Za-z]+)*)?)\s+du\s+CGI",
    # "articles X et Y du CGI" / "articles X à Y du CGI"
    r"articles?\s+([\d]+(?:[- ]\d+)?(?:\s*[A-Za-z]+(?:\s+[A-Za-z]+)*)?)\s+(?:et|à|,)\s+[\d]+[^d]*du\s+(?:CGI|code\s+g[ée]n[ée]ral\s+des\s+imp[oô]ts)",
]

# PLF/PLFSS article patterns (these are NOT CGI articles)
PLF_PATTERNS = [
    r"article\s+\d+\s+du\s+projet\s+de\s+loi",
    r"article\s+\d+\s+du\s+PLF",
    r"article\s+\d+\s+du\s+PLFSS",
]


def normalize_article_name(name: str) -> str:
    """Normalize article name for comparison."""
    name = name.strip()
    # Normalize unicode
    name = unicodedata.normalize("NFC", name)
    # Normalize spaces and dashes
    name = re.sub(r"\s+", " ", name)
    return name


def build_article_name_variants(article_name: str) -> list[str]:
    """Build various forms of an article name for text matching."""
    variants = [article_name]
    # Without dashes
    variants.append(article_name.replace("-", " "))
    # With spaces around dashes
    variants.append(article_name.replace("-", " - "))
    # Lowercase
    variants.extend([v.lower() for v in variants])
    # "article X" forms
    for v in list(variants):
        variants.append(f"article {v}")
        variants.append(f"l'article {v}")
    return list(set(variants))


def citation_mentions_article(citation: str, article_name: str) -> bool:
    """Check if citation text mentions the given article name."""
    citation_lower = citation.lower()
    variants = build_article_name_variants(article_name)
    for variant in variants:
        if variant.lower() in citation_lower:
            return True
    return False


def extract_cgi_references(citation: str) -> list[str]:
    """Extract CGI article references from citation text."""
    refs = []
    for pattern in CGI_PATTERNS:
        matches = re.finditer(pattern, citation, re.IGNORECASE)
        for m in matches:
            ref = normalize_article_name(m.group(1))
            refs.append(ref)
    return refs


def is_plf_reference(citation: str) -> bool:
    """Check if citation is primarily about a PLF/PLFSS article."""
    for pattern in PLF_PATTERNS:
        if re.search(pattern, citation, re.IGNORECASE):
            return True
    return False


def is_procedural_noise(citation: str) -> bool:
    """Check if citation is procedural noise."""
    noise_indicators = [
        "je mets aux voix",
        "l'amendement est adopté",
        "l'amendement est rejeté",
        "la parole est à",
        "je suis saisi",
        "quel est l'avis",
        "avis favorable",
        "avis défavorable",
        "l'amendement n°",
        "lecture de l'amendement",
    ]
    citation_lower = citation.lower()
    matches = sum(1 for ind in noise_indicators if ind in citation_lower)
    # If more than half the text is procedural
    if matches >= 2:
        return True
    # Very short citations that are just procedural
    if len(citation) < 100 and matches >= 1:
        return True
    return False


def extract_titre_keywords(titre: str) -> set[str]:
    """Extract meaningful keywords from article titre."""
    if not titre:
        return set()
    # Remove common stopwords
    stopwords = {
        "de", "du", "des", "le", "la", "les", "l", "d", "un", "une",
        "et", "ou", "en", "à", "au", "aux", "par", "pour", "sur",
        "dans", "avec", "qui", "que", "dont", "ce", "cette", "ces",
        "son", "sa", "ses", "leur", "leurs", "est", "sont", "a",
        "article", "articles", "code", "général", "impôts", "cgi",
        "régime", "dispositions", "conditions", "cas", "application",
        "titre", "chapitre", "section", "alinéa", "paragraphe",
    }
    # Tokenize
    words = re.findall(r"[a-zéèêëàâäùûüîïôöç]+", titre.lower())
    keywords = {w for w in words if len(w) > 2 and w not in stopwords}
    return keywords


def keyword_overlap_score(citation: str, keywords: set[str]) -> float:
    """Score how well a citation matches a set of keywords."""
    if not keywords:
        return 0.0
    citation_lower = citation.lower()
    matches = sum(1 for kw in keywords if kw in citation_lower)
    return matches / len(keywords)


def find_best_article_match(ref: str, article_index: dict) -> str | None:
    """Try to find the best matching article name in the index for a reference."""
    # Direct match
    if ref in article_index:
        return ref

    # Normalize and try variants
    ref_normalized = ref.strip()

    # Try with different dash/space patterns
    for art_name in article_index:
        art_lower = art_name.lower().replace("-", " ").replace("  ", " ").strip()
        ref_lower = ref_normalized.lower().replace("-", " ").replace("  ", " ").strip()
        if art_lower == ref_lower:
            return art_name

    # Try prefix match for complex article names (e.g., "200 A" matching "200 A")
    for art_name in article_index:
        if art_name.startswith(ref_normalized) or ref_normalized.startswith(art_name):
            # Only if they share the numeric prefix
            ref_num = re.match(r"(\d+)", ref_normalized)
            art_num = re.match(r"(\d+)", art_name)
            if ref_num and art_num and ref_num.group(1) == art_num.group(1):
                # Check if rest also matches closely
                if len(ref_normalized) >= len(art_name) * 0.8:
                    return art_name

    return None


def classify_quote(
    source_article: str,
    source_titre: str,
    target_article: str,
    target_titre: str,
    citation: str,
    article_index: dict,
) -> str:
    """Classify a potentially misplaced quote as 'move', 'delete', or 'keep'."""

    # If citation mentions the source article, it's likely a false positive
    if citation_mentions_article(citation, source_article):
        return "keep"

    # If it's about a PLF article, delete
    if is_plf_reference(citation):
        return "delete"

    # If it's procedural noise, delete
    if is_procedural_noise(citation):
        return "delete"

    # Compare keyword overlap with source vs target titre
    source_keywords = extract_titre_keywords(source_titre)
    target_keywords = extract_titre_keywords(target_titre)

    source_score = keyword_overlap_score(citation, source_keywords)
    target_score = keyword_overlap_score(citation, target_keywords)

    # If target scores significantly better, move
    if target_score > source_score + 0.05 and target_score > 0.1:
        return "move"

    # If neither matches well and citation doesn't mention source,
    # check if it clearly discusses the target article's topic
    if source_score < 0.05 and target_score > 0.1:
        return "move"

    # If target doesn't exist in index or has no titre
    if not target_titre:
        return "delete"

    # If scores are similar but both low, and source isn't mentioned —
    # be conservative, keep it
    if source_score < 0.05 and target_score < 0.05:
        # Neither matches well. If short/noisy, delete. Otherwise keep.
        if len(citation) < 80:
            return "delete"
        return "keep"

    # Default: keep (conservative)
    return "keep"


def main():
    print("Loading article_modifications.json...")
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    articles = data["articles"]
    print(f"Loaded {len(articles)} articles")

    # Build detection results
    detections = []  # (source_art, year, quote_idx, target_art, citation)

    for art_name, art_data in articles.items():
        curated = art_data.get("curated_quotes", {})
        if not curated:
            continue

        for year, quotes in curated.items():
            for idx, quote in enumerate(quotes):
                citation = quote.get("citation", "")
                if not citation:
                    continue

                # Extract CGI references
                cgi_refs = extract_cgi_references(citation)
                if not cgi_refs:
                    continue

                for ref in cgi_refs:
                    # Find matching article in index
                    target = find_best_article_match(ref, articles)
                    if target is None:
                        continue

                    # Skip self-references
                    if target == art_name:
                        continue

                    # Check if citation mentions source article
                    if citation_mentions_article(citation, art_name):
                        continue  # False positive, skip

                    detections.append({
                        "source": art_name,
                        "year": year,
                        "quote_idx": idx,
                        "target": target,
                        "citation": citation,
                        "orateur": quote.get("orateur", ""),
                    })

    print(f"\nFound {len(detections)} potential misplacements")

    # Deduplicate: same (source, year, idx) may match multiple targets
    # Keep the first target found
    seen = set()
    unique_detections = []
    for d in detections:
        key = (d["source"], d["year"], d["quote_idx"])
        if key not in seen:
            seen.add(key)
            unique_detections.append(d)

    print(f"After dedup: {len(unique_detections)} unique quotes to classify")

    # Classify each detection
    actions = {"move": [], "delete": [], "keep": []}

    for d in unique_detections:
        source_titre = articles[d["source"]].get("titre", "")
        target_titre = articles[d["target"]].get("titre", "")

        action = classify_quote(
            source_article=d["source"],
            source_titre=source_titre,
            target_article=d["target"],
            target_titre=target_titre,
            citation=d["citation"],
            article_index=articles,
        )
        d["action"] = action
        actions[action].append(d)

    # Print classification summary
    print(f"\n{'='*60}")
    print(f"Classification Results:")
    print(f"  MOVE:   {len(actions['move'])}")
    print(f"  DELETE: {len(actions['delete'])}")
    print(f"  KEEP:   {len(actions['keep'])}")
    print(f"{'='*60}")

    # Print details
    for action_type in ["move", "delete", "keep"]:
        if actions[action_type]:
            print(f"\n--- {action_type.upper()} ({len(actions[action_type])}) ---")
            for d in actions[action_type]:
                citation_preview = d["citation"][:120].replace("\n", " ")
                if action_type == "move":
                    print(f"  [{d['source']}] → [{d['target']}] (year={d['year']})")
                elif action_type == "delete":
                    print(f"  [{d['source']}] ✗ (ref: {d['target']}, year={d['year']})")
                else:
                    print(f"  [{d['source']}] (ref: {d['target']}, year={d['year']})")
                print(f"    \"{citation_preview}...\"")
                print()

    # Execute moves and deletes
    # We need to process removals in reverse index order to not shift indices
    # Group by (source, year) and collect indices to remove
    removals = defaultdict(list)  # (source, year) -> [idx, ...]
    additions = defaultdict(list)  # (target, year) -> [quote, ...]

    for d in actions["move"] + actions["delete"]:
        removals[(d["source"], d["year"])].append(d["quote_idx"])

    for d in actions["move"]:
        # Get the full quote from the source
        quote = articles[d["source"]]["curated_quotes"][d["year"]][d["quote_idx"]]
        additions[(d["target"], d["year"])].append(quote)

    # Perform removals (reverse order to preserve indices)
    removed_count = 0
    for (source, year), indices in removals.items():
        indices_sorted = sorted(set(indices), reverse=True)
        curated = articles[source]["curated_quotes"]
        for idx in indices_sorted:
            if year in curated and idx < len(curated[year]):
                curated[year].pop(idx)
                removed_count += 1
        # Clean up empty year lists
        if year in curated and len(curated[year]) == 0:
            del curated[year]

    # Perform additions
    added_count = 0
    for (target, year), quotes in additions.items():
        curated = articles[target].setdefault("curated_quotes", {})
        if year not in curated:
            curated[year] = []
        for quote in quotes:
            # Avoid duplicates
            existing_citations = {q.get("citation", "") for q in curated[year]}
            if quote.get("citation", "") not in existing_citations:
                curated[year].append(quote)
                added_count += 1

    print(f"\n{'='*60}")
    print(f"Execution Summary:")
    print(f"  Quotes removed from source articles: {removed_count}")
    print(f"  Quotes added to target articles:     {added_count}")
    print(f"  Quotes kept in place (false pos.):   {len(actions['keep'])}")
    print(f"{'='*60}")

    # Save
    print(f"\nSaving to {DATA_PATH}...")
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("Done!")


if __name__ == "__main__":
    main()
