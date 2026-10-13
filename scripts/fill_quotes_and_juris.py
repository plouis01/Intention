#!/usr/bin/env python3
"""
Fill missing curated_quotes from CGI mention index + generate jurisprudence.
No external LLM required - uses keyword filtering for quotes and
pre-built jurisprudence data.
"""
import json
import re
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ──────────────────────────────────────────────────────────────────
# CURATED QUOTES EXTRACTION
# ──────────────────────────────────────────────────────────────────

def build_relevance_keywords(article_ref: str, article_data: dict) -> set:
    """Build keywords that indicate an intervention is actually about this CGI article."""
    keywords = set()

    # The article reference itself (various forms)
    ref = article_ref.strip()
    keywords.add(ref.lower())
    keywords.add(f"article {ref}".lower())
    keywords.add(f"l'article {ref}".lower())

    # From article keywords
    for kw in article_data.get('keywords', []):
        kw = kw.strip().lower()
        if len(kw) > 4 and kw not in {
            'article', 'impôt', 'impôts', 'impot', 'fiscal', 'fiscale',
            'régime', 'regime', 'société', 'societe', 'cession', 'nature',
            'titres', 'revenus', 'revenu', 'montant', 'sommes', 'droit',
            'droits', 'code', 'général', 'general', 'dispositions',
            'conditions', 'application', 'cadre', 'mesure', 'mesures',
        }:
            keywords.add(kw)

    # From titre
    titre = article_data.get('titre', '').lower()
    # Extract substantive words from title
    for word in re.findall(r'[a-zàâäéèêëïîôùûüÿç-]{5,}', titre):
        if word not in {
            'article', 'impôt', 'impôts', 'fiscal', 'fiscale', 'annexe',
            'régime', 'général', 'dispositions', 'conditions', 'autres',
            'relative', 'relatif', 'relatives', 'application', 'entre',
            'notamment', 'certains', 'certaines', 'partir', 'depuis',
        }:
            keywords.add(word)

    return keywords


def score_mention(mention: dict, article_ref: str, keywords: set) -> float:
    """Score a CGI mention for relevance to the article. Higher = more relevant."""
    snippet = mention.get('snippet', '').lower()
    score = 0.0

    # Must mention the article reference explicitly
    ref_lower = article_ref.lower()
    if f"article {ref_lower}" in snippet or f"l'article {ref_lower}" in snippet:
        score += 3.0
    elif ref_lower in snippet:
        score += 1.0
    else:
        return 0  # No mention of this article at all

    # Bonus for keyword matches
    kw_matches = sum(1 for kw in keywords if kw in snippet)
    score += min(kw_matches * 0.5, 3.0)

    # Bonus for substantive speakers (rapporteur, ministre)
    fonction = mention.get('fonction', '').lower()
    if 'rapporteur' in fonction:
        score += 2.0
    elif 'ministre' in fonction:
        score += 1.5
    elif 'president' in fonction or 'présidente' in fonction:
        score -= 1.0  # Procedural

    # Penalty for very short snippets
    if len(snippet) < 100:
        score -= 1.0

    # Bonus for longer, more substantive snippets
    if len(snippet) > 300:
        score += 1.0

    # Penalty for procedural language
    procedural = ['amendement', 'sous-amendement', 'scrutin', 'vote', 'adopté',
                  'rejeté', 'retiré', 'la parole est à', 'quel est l\'avis']
    proc_count = sum(1 for p in procedural if p in snippet)
    score -= proc_count * 0.3

    return score


def extract_best_quotes(article_ref: str, article_data: dict,
                        cgi_mentions: list, max_per_law: int = 2) -> dict:
    """Extract best quotes from CGI mentions, grouped by law year."""
    if not cgi_mentions:
        return {}

    keywords = build_relevance_keywords(article_ref, article_data)

    # Score all mentions
    scored = []
    for m in cgi_mentions:
        s = score_mention(m, article_ref, keywords)
        if s > 2.0:  # Minimum quality threshold
            scored.append((s, m))

    if not scored:
        return {}

    # Sort by score descending
    scored.sort(key=lambda x: -x[0])

    # Group by year and pick the best per year
    quotes_by_year = {}
    for score, m in scored:
        date = m.get('debat_date', '')
        year = date[:4] if date else None
        if not year:
            continue

        if year not in quotes_by_year:
            quotes_by_year[year] = []

        if len(quotes_by_year[year]) < max_per_law:
            snippet = m.get('snippet', '')
            # Clean up snippet
            if len(snippet) > 500:
                snippet = snippet[:497] + "..."

            quotes_by_year[year].append({
                "citation": snippet,
                "orateur": m.get('orateur', ''),
                "fonction": m.get('fonction', ''),
                "date": date,
                "chambre": m.get('chambre', '')
            })

    return quotes_by_year


def fill_curated_quotes(articles: dict, cgi_index: dict) -> int:
    """Fill curated_quotes for articles that are missing them."""
    filled = 0
    for art_ref, art_data in articles.items():
        # Skip if already has quotes
        cq = art_data.get('curated_quotes')
        if isinstance(cq, dict) and len(cq) > 0:
            continue

        mentions = cgi_index.get(art_ref, [])
        if not mentions:
            continue

        quotes = extract_best_quotes(art_ref, art_data, mentions)
        if quotes:
            art_data['curated_quotes'] = quotes
            filled += 1

    return filled


# ──────────────────────────────────────────────────────────────────
# JURISPRUDENCE FROM DB INTERVENTIONS
# ──────────────────────────────────────────────────────────────────

def extract_jurisprudence_from_mentions(article_ref: str, article_data: dict,
                                         cgi_mentions: list) -> list:
    """Try to extract jurisprudence references from debate mentions.

    Parliamentary debates often cite case law when discussing CGI articles.
    Look for patterns like 'Conseil d'État', 'Cour de cassation', etc.
    """
    if not cgi_mentions:
        return []

    juris_patterns = [
        (r"Conseil\s+d[''']État", "Conseil d'État"),
        (r"Conseil\s+constitutionnel", "Conseil constitutionnel"),
        (r"Cour\s+de\s+cassation", "Cour de cassation"),
        (r"CJUE|Cour\s+de\s+justice\s+de\s+l[''']Union", "CJUE"),
        (r"cour\s+administrative\s+d[''']appel|CAA", "Cour administrative d'appel"),
    ]

    found_juris = []
    seen = set()

    for m in cgi_mentions:
        snippet = m.get('snippet', '')
        for pattern, juridiction in juris_patterns:
            if re.search(pattern, snippet, re.IGNORECASE):
                # Try to extract a case number
                numero_match = re.search(
                    r'n[°o]\s*(\d[\d\-/.]+\d)',
                    snippet
                )
                numero = numero_match.group(1) if numero_match else None

                # Try to extract a date
                date_match = re.search(
                    r'(\d{1,2}\s+(?:janvier|février|mars|avril|mai|juin|juillet|'
                    r'août|septembre|octobre|novembre|décembre)\s+\d{4})',
                    snippet, re.IGNORECASE
                )
                date_str = date_match.group(1) if date_match else None

                key = (juridiction, numero or date_str or snippet[:50])
                if key in seen:
                    continue
                seen.add(key)

                # Build a resume from the snippet context
                resume = snippet.strip()
                if len(resume) > 300:
                    resume = resume[:297] + "..."

                found_juris.append({
                    "juridiction": juridiction,
                    "date": date_str,
                    "reference": numero or "",
                    "titre": f"{juridiction}, {date_str or 'date inconnue'}" + (f", n° {numero}" if numero else ""),
                    "faits": "",
                    "solution": resume,
                    "portee": "",
                })

    return found_juris[:3]  # Max 3 per article


def fill_jurisprudence(articles: dict, cgi_index: dict) -> int:
    """Fill jurisprudence for articles that are missing them."""
    filled = 0
    for art_ref, art_data in articles.items():
        if art_data.get('jurisprudence'):
            continue

        mentions = cgi_index.get(art_ref, [])
        juris = extract_jurisprudence_from_mentions(art_ref, art_data, mentions)
        if juris:
            art_data['jurisprudence'] = juris
            filled += 1

    return filled


# ──────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────

def main():
    print("Loading data...")
    with open(os.path.join(PROJECT_ROOT, 'data', 'article_modifications.json')) as f:
        data = json.load(f)
    articles = data['articles']

    with open(os.path.join(PROJECT_ROOT, 'data', 'cgi_mention_index.json')) as f:
        cgi_index = json.load(f)

    # Stats before
    has_quotes_before = sum(1 for a in articles.values()
                           if isinstance(a.get('curated_quotes'), dict) and len(a['curated_quotes']) > 0)
    has_juris_before = sum(1 for a in articles.values() if a.get('jurisprudence'))

    print(f"\nBefore:")
    print(f"  curated_quotes: {has_quotes_before}/{len(articles)}")
    print(f"  jurisprudence:  {has_juris_before}/{len(articles)}")

    # Fill quotes
    print("\nExtracting curated_quotes from CGI mention index...")
    quotes_filled = fill_curated_quotes(articles, cgi_index)

    # Fill jurisprudence from debate mentions
    print("Extracting jurisprudence from debate mentions...")
    juris_filled = fill_jurisprudence(articles, cgi_index)

    # Stats after
    has_quotes_after = sum(1 for a in articles.values()
                          if isinstance(a.get('curated_quotes'), dict) and len(a['curated_quotes']) > 0)
    has_juris_after = sum(1 for a in articles.values() if a.get('jurisprudence'))

    print(f"\nAfter:")
    print(f"  curated_quotes: {has_quotes_after}/{len(articles)} (+{quotes_filled})")
    print(f"  jurisprudence:  {has_juris_after}/{len(articles)} (+{juris_filled})")

    # Save
    with open(os.path.join(PROJECT_ROOT, 'data', 'article_modifications.json'), 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\nSaved to data/article_modifications.json")


if __name__ == '__main__':
    main()
