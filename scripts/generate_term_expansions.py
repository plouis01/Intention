#!/usr/bin/env python3
"""Generate legal term expansions for CGI articles using Claude API."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
from src.utils.config import get_settings


SYSTEM_PROMPT = """Tu es un expert en droit fiscal francais, specialise dans le Code General des Impots (CGI).

Pour chaque article du CGI fourni, tu dois generer une liste de termes de recherche associes qui permettront de retrouver les debats parlementaires pertinents.

Les termes doivent inclure:
1. Les variantes orthographiques de la reference de l'article (avec/sans espaces, tirets)
2. Le nom usuel du dispositif s'il existe
3. Les concepts juridiques cles du dispositif
4. Les mecanismes fiscaux concernes
5. Les termes techniques utilises dans les debats parlementaires
6. Les problematiques recurrentes associees au dispositif

IMPORTANT:
- Genere uniquement des termes en francais
- Utilise des apostrophes droites (') et non typographiques
- Evite les accents dans les termes (pour faciliter la recherche)
- Limite-toi a 15-20 termes maximum par article
- Les termes doivent etre en minuscules"""


USER_PROMPT_TEMPLATE = """Genere les termes de recherche associes pour l'article suivant du CGI:

Reference: {reference}
Titre: {titre}
Theme: {theme}

Reponds UNIQUEMENT avec un JSON valide au format:
{{"terms": ["terme1", "terme2", "terme3", ...]}}"""


def generate_expansions_for_article(
    client: anthropic.Anthropic,
    article: dict,
) -> list[str]:
    """Generate term expansions for a single CGI article using Claude."""
    prompt = USER_PROMPT_TEMPLATE.format(
        reference=article["reference"],
        titre=article["titre"],
        theme=article["theme"],
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    # Parse the JSON response
    response_text = response.content[0].text.strip()

    # Handle potential markdown code blocks
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        response_text = "\n".join(lines[1:-1])

    try:
        data = json.loads(response_text)
        return data.get("terms", [])
    except json.JSONDecodeError as e:
        print(f"  Warning: Failed to parse response for {article['reference']}: {e}")
        print(f"  Response was: {response_text[:200]}...")
        return []


def load_reference_articles(path: Path) -> list[dict]:
    """Load the reference articles from JSON file."""
    with open(path) as f:
        data = json.load(f)
    return [a for a in data["articles"] if a.get("generate_expansions", True)]


def save_expansions(expansions: dict, path: Path) -> None:
    """Save the generated expansions to JSON file."""
    output = {
        "description": "Legal term expansions for CGI article searches",
        "generated_by": "scripts/generate_term_expansions.py",
        "note": "Terms are normalized (lowercase, straight apostrophes, no accents for search)",
        "expansions": expansions,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


def main():
    """Generate term expansions for all reference CGI articles."""
    settings = get_settings()

    # Check for API key
    if not settings.anthropic_api_key:
        print("Error: ANTHROPIC_API_KEY not set in environment")
        sys.exit(1)

    # Paths
    reference_path = Path("data/cgi_articles_reference.json")
    output_path = Path("data/legal_term_expansions.json")

    # Load reference articles
    print("Loading reference articles...")
    articles = load_reference_articles(reference_path)
    print(f"  Found {len(articles)} articles to process")

    # Load existing expansions if any (to support incremental generation)
    existing_expansions = {}
    if output_path.exists():
        with open(output_path) as f:
            data = json.load(f)
            existing_expansions = data.get("expansions", {})
        print(f"  Loaded {len(existing_expansions)} existing expansions")

    # Initialize Anthropic client
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    # Generate expansions
    expansions = existing_expansions.copy()

    for i, article in enumerate(articles, 1):
        ref = article["reference"]

        # Skip if already generated (unless forced)
        if ref in expansions:
            print(f"[{i}/{len(articles)}] Skipping {ref} (already exists)")
            continue

        print(f"[{i}/{len(articles)}] Generating terms for: {ref}")

        try:
            terms = generate_expansions_for_article(client, article)

            if terms:
                # Normalize terms
                normalized = []
                for term in terms:
                    # Lowercase
                    t = term.lower()
                    # Straight apostrophes
                    t = t.replace("\u2019", "'").replace("\u2018", "'")
                    normalized.append(t)

                expansions[ref] = normalized
                print(f"  -> Generated {len(normalized)} terms")
            else:
                print(f"  -> Warning: No terms generated")

        except Exception as e:
            print(f"  -> Error: {e}")

    # Save results
    print(f"\nSaving {len(expansions)} article expansions to {output_path}")
    save_expansions(expansions, output_path)

    # Print summary
    print("\n=== SUMMARY ===")
    total_terms = sum(len(terms) for terms in expansions.values())
    print(f"Articles: {len(expansions)}")
    print(f"Total terms: {total_terms}")
    print(f"Average terms per article: {total_terms / len(expansions):.1f}")


if __name__ == "__main__":
    main()
