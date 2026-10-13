#!/usr/bin/env python3
"""Build golden evaluation dataset from article_modifications.json curated data."""

import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

TARGET_ARTICLES = [
    "150-0 B ter", "787 B", "199 terdecies-0 A", "200 A", "219",
    "44 sexies A", "39", "238 bis", "150-0 A", "885 I bis",
    "200", "44 sexies", "217 undecies", "238 quater", "1649 quater B quater",
]

# Known contamination patterns for false-positive-prone articles
CONTAMINATION_PATTERNS = {
    "150-0 B ter": [
        {"content_substring": "taxe d'habitation", "reason": "Unrelated tax topic"},
        {"content_substring": "taxe foncière", "reason": "Unrelated property tax"},
    ],
    "150-0 A": [
        {"content_substring": "article 150-0 B ter", "reason": "Different article"},
        {"content_substring": "article 150 U", "reason": "Different article"},
    ],
    "44 bis": [
        {"content_substring": "article 44 quater", "reason": "Different article"},
        {"content_substring": "article 44 ter", "reason": "Different article"},
        {"content_substring": "article 44 sexies", "reason": "Different article"},
    ],
    "44 sexies": [
        {"content_substring": "article 44 bis", "reason": "Different article"},
        {"content_substring": "article 44 quater", "reason": "Different article"},
        {"content_substring": "article 44 sexies A", "reason": "Different article variant"},
    ],
    "39": [
        {"content_substring": "article 39 bis", "reason": "Different article variant"},
        {"content_substring": "article 39 A", "reason": "Different article variant"},
        {"content_substring": "article 39 AA", "reason": "Different article variant"},
    ],
    "200": [
        {"content_substring": "article 200 A", "reason": "Different article"},
        {"content_substring": "article 200 quater", "reason": "Different article"},
        {"content_substring": "article 200 B", "reason": "Different article"},
    ],
    "200 A": [
        {"content_substring": "article 200 quater", "reason": "Different article"},
        {"content_substring": "article 200 B", "reason": "Different article"},
    ],
    "217 undecies": [
        {"content_substring": "article 217 octies", "reason": "Different article"},
    ],
    "238 bis": [
        {"content_substring": "article 238 bis AB", "reason": "Different article"},
        {"content_substring": "article 238 quater", "reason": "Different article"},
    ],
    "885 I bis": [
        {"content_substring": "article 885 I ter", "reason": "Different article"},
    ],
    "1649 quater B quater": [
        {"content_substring": "1649 quater B bis", "reason": "Different article"},
        {"content_substring": "1649 quater B ter", "reason": "Different article"},
    ],
}


def build_golden_set():
    index_path = Path("data/article_modifications.json")
    if not index_path.exists():
        print(f"ERROR: {index_path} not found")
        return

    with open(index_path) as f:
        data = json.load(f)

    articles = data.get("articles", {})
    cases = []

    for article_ref in TARGET_ARTICLES:
        if article_ref not in articles:
            print(f"  SKIP: {article_ref} not in index")
            continue

        art_data = articles[article_ref]
        curated_quotes = art_data.get("curated_quotes", {})
        keywords = art_data.get("keywords", [])

        if not curated_quotes:
            print(f"  SKIP: {article_ref} has no curated_quotes")
            continue

        # Build cases from curated_quotes keyed by year
        for year_str, quotes in curated_quotes.items():
            if not quotes:
                continue

            year = int(year_str)
            # Determine law type from modifications
            law_type = "LF"
            for mod in art_data.get("modifications", []):
                if mod.get("law_year") == year:
                    law_type = mod.get("law_type", "LF")
                    break

            # Build must_find from curated quotes
            must_find = []
            expected_quotes = []
            has_authoritative = False

            for q in quotes:
                citation = q.get("citation", "")
                orateur = q.get("orateur", "")
                chambre = q.get("chambre", "")
                fonction = q.get("fonction", "")

                if len(citation) < 30:
                    continue

                # Use a meaningful substring (first 80 chars trimmed)
                substring = citation[:80].strip()
                must_find.append({
                    "content_substring": substring,
                    "speaker": orateur,
                })
                expected_quotes.append({
                    "citation_substring": substring,
                    "orateur": orateur,
                    "chambre": chambre,
                })

                if any(r in (fonction or "").lower() for r in ("rapporteur", "ministre")):
                    has_authoritative = True

            if not must_find:
                continue

            # Contamination patterns
            must_not_find = CONTAMINATION_PATTERNS.get(article_ref, [])

            # Expected certitude
            expected_certitude = "eleve" if has_authoritative else "moyen"

            # Extract relevant intention keywords (top 5 non-generic)
            generic_terms = {"société", "régime", "impôt", "cession", "titres", "article"}
            intention_keywords = [
                kw for kw in keywords[:10]
                if kw.lower() not in generic_terms and len(kw) > 4
            ][:5]

            sanitized = article_ref.replace(' ', '_').replace('-', '_').lower()
            case_id = f"{sanitized}_{law_type.lower()}_{year}"
            cases.append({
                "id": case_id,
                "article_ref": article_ref,
                "law_type": law_type,
                "law_year": year,
                "must_find": must_find,
                "must_not_find": must_not_find,
                "expected_quotes": expected_quotes,
                "expected_intention_keywords": intention_keywords,
                "expected_certitude": expected_certitude,
            })

    # Write output
    output_dir = Path("data/eval")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "golden_set.json"

    result = {
        "description": "Golden evaluation dataset for article analysis pipeline",
        "version": "1.0",
        "total_cases": len(cases),
        "cases": cases,
    }

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"Generated {len(cases)} golden cases → {output_path}")
    # Summary
    articles_covered = set(c["article_ref"] for c in cases)
    print(f"Articles covered: {len(articles_covered)}/{len(TARGET_ARTICLES)}")
    for art in sorted(articles_covered):
        art_cases = [c for c in cases if c["article_ref"] == art]
        print(f"  {art}: {len(art_cases)} cases")


if __name__ == "__main__":
    build_golden_set()
