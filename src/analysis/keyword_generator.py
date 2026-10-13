"""Automatic keyword generation for CGI article searches.

This module generates search keywords automatically from:
1. Article reference variants (e.g., "150-0 B ter" -> "150 0 B ter", "article 150-0 B ter")
2. Legal terminology associated with the article
3. Key phrases from law article content
"""

import json
import logging
import re
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# Latin ordinals used in CGI article numbering
ORDINALS = [
    "bis", "ter", "quater", "quinquies", "sexies", "septies", "octies",
    "nonies", "decies", "undecies", "duodecies", "terdecies", "quaterdecies",
    "quindecies", "sexdecies"
]


def generate_reference_variants(article_ref: str) -> list[str]:
    """
    Generate all common variants of a CGI article reference.

    Args:
        article_ref: Normalized article reference (e.g., "150-0 B ter")

    Returns:
        List of variant strings for search
    """
    variants = set()

    # Original reference
    variants.add(article_ref)

    # Lowercase version
    variants.add(article_ref.lower())

    # With "article" prefix
    variants.add(f"article {article_ref}")
    variants.add(f"article {article_ref.lower()}")
    variants.add(f"l'article {article_ref}")
    variants.add(f"l'article {article_ref.lower()}")

    # Replace hyphens with spaces
    no_hyphen = article_ref.replace("-", " ")
    variants.add(no_hyphen)
    variants.add(no_hyphen.lower())
    variants.add(f"article {no_hyphen}")
    variants.add(f"article {no_hyphen.lower()}")

    # Remove all separators (compact form)
    compact = article_ref.replace("-", "").replace(" ", "")
    variants.add(compact)
    variants.add(compact.lower())

    # For articles with ordinals, add variant with ordinal first
    # e.g., "244 quater B" is already correct, but ensure "quater 244" variant exists
    for ordinal in ORDINALS:
        if ordinal in article_ref.lower():
            # Split around ordinal
            parts = re.split(rf'\b{ordinal}\b', article_ref, flags=re.IGNORECASE)
            if len(parts) == 2:
                num_part = parts[0].strip()
                letter_part = parts[1].strip()
                if num_part and letter_part:
                    # "244 quater B" -> "quater 244 B"
                    reordered = f"{ordinal} {num_part} {letter_part}".strip()
                    variants.add(reordered)
                    variants.add(reordered.lower())

    # For articles with number suffixes like "150-0 B ter"
    # Add variant with just the base: "150" and "150 B ter"
    base_match = re.match(r'^(\d+)', article_ref)
    if base_match:
        base = base_match.group(1)
        if base != article_ref:
            variants.add(base)

    # Remove duplicates and sort
    return sorted(set(v for v in variants if v))


def extract_keywords_from_titre(titre: str) -> list[str]:
    """
    Extract keywords from an article's title/description.

    Args:
        titre: Article title (e.g., "Report d'imposition des plus-values...")

    Returns:
        List of extracted keywords
    """
    if not titre:
        return []

    keywords = []

    # Clean and normalize
    titre_lower = titre.lower()

    # Extract significant phrases (2-4 words)
    # Remove common words
    stopwords = {
        "le", "la", "les", "de", "du", "des", "un", "une", "et", "ou",
        "pour", "par", "sur", "en", "aux", "a", "à", "d'", "l'",
        "dans", "avec", "sans", "son", "sa", "ses", "leur", "leurs",
        "qui", "que", "quoi", "dont", "où", "ce", "cette", "ces",
        "être", "avoir", "faire", "est", "sont", "fait"
    }

    # Split on common separators
    parts = re.split(r'[\s\-–—/,;:()]+', titre_lower)
    words = [w for w in parts if w and w not in stopwords and len(w) > 2]

    # Add individual significant words
    keywords.extend(words)

    # Add 2-word phrases
    for i in range(len(words) - 1):
        phrase = f"{words[i]} {words[i+1]}"
        keywords.append(phrase)

    # Extract parenthetical terms (often abbreviations)
    parens = re.findall(r'\(([^)]+)\)', titre)
    for p in parens:
        keywords.append(p.lower())

    return list(set(keywords))


def extract_keywords_from_law_text(text: str, article_ref: str) -> list[str]:
    """
    Extract keywords from law text that modifies this CGI article.

    Args:
        text: Law article content
        article_ref: CGI article reference being modified

    Returns:
        List of extracted keywords
    """
    if not text:
        return []

    keywords = []

    # Look for key legal phrases
    legal_phrases = [
        r"(crédit d'impôt\w*)",
        r"(réduction d'impôt\w*)",
        r"(exonération\w*)",
        r"(abattement\w*)",
        r"(report d'imposition)",
        r"(sursis d'imposition)",
        r"(plus-value\w*)",
        r"(moins-value\w*)",
        r"(cession de titres)",
        r"(apport de titres)",
        r"(transmission d'entreprise)",
        r"(droits de succession)",
        r"(pacte [Dd]utreil)",
        r"(flat tax|PFU|prélèvement forfaitaire)",
        r"(IFI|ISF|impôt sur la fortune)",
        r"(holding|société holding)",
    ]

    text_lower = text.lower()
    for pattern in legal_phrases:
        matches = re.findall(pattern, text_lower)
        keywords.extend(matches)

    # Extract any mentioned percentages or amounts (often significant)
    percentages = re.findall(r'(\d+(?:,\d+)?\s*%)', text)
    keywords.extend(percentages)

    # Look for "au sens de" definitions
    definitions = re.findall(r'au sens (?:de|du|des) ([^,\.]+)', text_lower)
    keywords.extend(definitions)

    return list(set(keywords))


def load_legal_term_expansions(path: Path = None) -> dict[str, list[str]]:
    """Load manually curated legal term expansions."""
    if path is None:
        path = Path(__file__).parent.parent.parent / "data" / "legal_term_expansions.json"

    if not path.exists():
        logger.warning(f"Legal term expansions file not found: {path}")
        return {}

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    return data.get("expansions", {})


def generate_all_keywords(
    article_ref: str,
    titre: str = "",
    law_text: str = "",
    include_curated: bool = True,
) -> list[str]:
    """
    Generate comprehensive keyword list for an article.

    Args:
        article_ref: CGI article reference
        titre: Article title/description
        law_text: Optional law text that modifies this article
        include_curated: Include manually curated expansions

    Returns:
        Deduplicated list of keywords
    """
    keywords = set()

    # 1. Reference variants
    keywords.update(generate_reference_variants(article_ref))

    # 2. Keywords from title
    if titre:
        keywords.update(extract_keywords_from_titre(titre))

    # 3. Keywords from law text
    if law_text:
        keywords.update(extract_keywords_from_law_text(law_text, article_ref))

    # 4. Curated expansions
    if include_curated:
        expansions = load_legal_term_expansions()
        if article_ref in expansions:
            keywords.update(expansions[article_ref])

    # Remove empty strings and very short keywords
    keywords = {k for k in keywords if k and len(k) > 1}

    return sorted(keywords)


def enrich_article_keywords(
    article_data: dict,
    article_ref: str,
    law_texts: dict[str, str] = None,
) -> dict:
    """
    Enrich an article's keywords with auto-generated ones.

    Args:
        article_data: Article data dict from article_modifications.json
        article_ref: CGI article reference
        law_texts: Optional dict mapping law_numero to text

    Returns:
        Updated article_data with enriched keywords
    """
    titre = article_data.get("titre", "")

    # Collect law texts for this article
    combined_law_text = ""
    if law_texts:
        for mod in article_data.get("modifications", []):
            law_num = mod.get("law_numero", "")
            if law_num in law_texts:
                combined_law_text += " " + law_texts[law_num]

    # Generate all keywords
    all_keywords = generate_all_keywords(
        article_ref=article_ref,
        titre=titre,
        law_text=combined_law_text,
        include_curated=True,
    )

    # Merge with existing keywords
    existing = set(article_data.get("keywords", []))
    merged = sorted(existing | set(all_keywords))

    article_data["keywords"] = merged
    return article_data


if __name__ == "__main__":
    # Test the generator
    test_articles = [
        ("150-0 B ter", "Report d'imposition des plus-values lors de l'apport de titres (apport-cession)"),
        ("244 quater B", "Crédit d'impôt recherche (CIR)"),
        ("787 B", "Pacte Dutreil - Exonération transmission d'entreprise"),
    ]

    for ref, titre in test_articles:
        print(f"\n{'='*60}")
        print(f"Article: {ref}")
        print(f"Titre: {titre}")
        print(f"{'='*60}")

        variants = generate_reference_variants(ref)
        print(f"\nReference variants ({len(variants)}):")
        for v in variants[:10]:
            print(f"  - {v}")
        if len(variants) > 10:
            print(f"  ... and {len(variants) - 10} more")

        all_kw = generate_all_keywords(ref, titre)
        print(f"\nAll keywords ({len(all_kw)}):")
        for kw in all_kw[:15]:
            print(f"  - {kw}")
        if len(all_kw) > 15:
            print(f"  ... and {len(all_kw) - 15} more")
