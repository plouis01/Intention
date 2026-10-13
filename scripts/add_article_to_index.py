#!/usr/bin/env python3
"""
Interactive tool to add CGI articles to the modification index.

This tool provides a guided workflow to add articles when the Légifrance API
is unavailable or not working properly.

Usage:
    python scripts/add_article_to_index.py "787 B"
    python scripts/add_article_to_index.py --template "150-0 D"
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


INDEX_PATH = project_root / "data" / "article_modifications.json"
EXPANSIONS_PATH = project_root / "data" / "legal_term_expansions.json"


def load_index() -> dict:
    """Load the article modifications index."""
    if INDEX_PATH.exists():
        with open(INDEX_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"articles": {}}


def save_index(index: dict) -> None:
    """Save the article modifications index."""
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def get_existing_keywords(article_ref: str) -> list[str]:
    """Get keywords from the term expansions file if they exist."""
    if EXPANSIONS_PATH.exists():
        with open(EXPANSIONS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        expansions = data.get("expansions", {})
        if article_ref in expansions:
            return expansions[article_ref]
    return []


def generate_template(article_ref: str) -> dict:
    """Generate a template for an article entry."""
    keywords = get_existing_keywords(article_ref)
    if not keywords:
        keywords = [
            article_ref,
            article_ref.lower(),
            article_ref.replace("-", " "),
            article_ref.replace(" ", "").lower(),
        ]

    return {
        "titre": f"[TITRE DE L'ARTICLE {article_ref}]",
        "code": "CGI",
        "legifrance_url": f"https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI[ID]",
        "bofip_url": "https://bofip.impots.gouv.fr/bofip/[ID]-PGP.html",
        "keywords": keywords,
        "modifications": [
            {
                "date": "YYYY-MM-DD",
                "law_numero": "YYYY-NNNN",
                "law_type": "LF|LFR|LFSS|LOI",
                "law_year": 0,
                "law_title": "[Titre de la loi]",
                "article_in_law": "Art. XX",
                "nature": "Création|Modification",
                "legifrance_url": ""
            }
        ],
        "jurisprudence": [],
        "synthese": {
            "objectif_constant": "",
            "preoccupations_recurrentes": []
        }
    }


def print_lookup_instructions(article_ref: str) -> None:
    """Print instructions for looking up article info on Légifrance."""
    print(f"""
================================================================================
COMMENT TROUVER LES INFORMATIONS POUR L'ARTICLE {article_ref}
================================================================================

1. LÉGIFRANCE - Article actuel:
   https://www.legifrance.gouv.fr/codes/id/LEGITEXT000006069577/
   - Cherchez "Article {article_ref}" dans le sommaire du CGI
   - Notez l'URL de l'article (contient LEGIARTI...)
   - Copiez le titre complet

2. LÉGIFRANCE - Historique des modifications:
   - Sur la page de l'article, cliquez sur "Versions" ou "Historique"
   - Notez chaque modification avec:
     * Date de promulgation de la loi
     * Numéro de la loi (ex: 2023-1322)
     * Type (LF, LFR, LFSS)
     * Année budgétaire
     * Numéro d'article dans la loi

3. BOFIP - Doctrine administrative:
   https://bofip.impots.gouv.fr/
   - Recherchez l'article pour trouver la documentation officielle

4. EUR-Lex / CURIA - Jurisprudence:
   - Conseil d'État, Conseil Constitutionnel
   - Recherchez les décisions mentionnant l'article

================================================================================
""")


def interactive_add(article_ref: str) -> None:
    """Interactively add an article to the index."""
    index = load_index()

    if article_ref in index.get("articles", {}):
        print(f"L'article {article_ref} existe déjà dans l'index.")
        response = input("Voulez-vous le remplacer? (o/N): ")
        if response.lower() != "o":
            return

    print_lookup_instructions(article_ref)

    print(f"\n=== Ajout de l'article {article_ref} ===\n")

    # Get basic info
    titre = input(f"Titre de l'article [{article_ref}]: ").strip()
    if not titre:
        titre = f"Article {article_ref} du CGI"

    legifrance_id = input("ID Légifrance (LEGIARTI...): ").strip()
    legifrance_url = f"https://www.legifrance.gouv.fr/codes/article_lc/{legifrance_id}" if legifrance_id else ""

    bofip_id = input("ID BOFIP (ex: 12040): ").strip()
    bofip_url = f"https://bofip.impots.gouv.fr/bofip/{bofip_id}-PGP.html" if bofip_id else ""

    # Keywords
    existing_keywords = get_existing_keywords(article_ref)
    if existing_keywords:
        print(f"\nMots-clés existants: {', '.join(existing_keywords[:5])}...")
        keywords = existing_keywords
    else:
        keywords_input = input("Mots-clés (séparés par des virgules): ").strip()
        keywords = [k.strip() for k in keywords_input.split(",") if k.strip()]
        if not keywords:
            keywords = [article_ref]

    # Modifications
    modifications = []
    print("\n=== Modifications ===")
    print("Entrez les modifications (appuyez sur Entrée sans rien écrire pour terminer)")

    while True:
        print(f"\n--- Modification {len(modifications) + 1} ---")
        date = input("Date (YYYY-MM-DD): ").strip()
        if not date:
            break

        law_numero = input("Numéro de loi (ex: 2023-1322): ").strip()
        law_type = input("Type (LF/LFR/LFSS/LOI): ").strip().upper()
        law_year = input("Année budgétaire: ").strip()
        law_title = input("Titre de la loi: ").strip()
        article_in_law = input("Article dans la loi (ex: Art. 24): ").strip()
        nature = "Création" if len(modifications) == 0 else input("Nature (Création/Modification): ").strip()

        modifications.append({
            "date": date,
            "law_numero": law_numero,
            "law_type": law_type,
            "law_year": int(law_year) if law_year.isdigit() else 0,
            "law_title": law_title,
            "article_in_law": article_in_law,
            "nature": nature or "Modification",
            "legifrance_url": ""
        })

    if not modifications:
        print("Aucune modification ajoutée. Abandon.")
        return

    # Build entry
    entry = {
        "titre": titre,
        "code": "CGI",
        "legifrance_url": legifrance_url,
        "bofip_url": bofip_url,
        "keywords": keywords,
        "modifications": modifications,
        "jurisprudence": [],
        "synthese": {
            "objectif_constant": "",
            "preoccupations_recurrentes": []
        }
    }

    # Show result
    print("\n=== Résultat ===")
    print(json.dumps({article_ref: entry}, ensure_ascii=False, indent=2))

    # Confirm
    response = input("\nSauvegarder? (O/n): ")
    if response.lower() != "n":
        index["articles"][article_ref] = entry
        save_index(index)
        print(f"\nArticle {article_ref} sauvegardé dans {INDEX_PATH}")


def show_template(article_ref: str) -> None:
    """Show a JSON template for the article."""
    template = generate_template(article_ref)
    print(f"\n=== Template pour {article_ref} ===\n")
    print(json.dumps({article_ref: template}, ensure_ascii=False, indent=2))
    print_lookup_instructions(article_ref)


def main():
    parser = argparse.ArgumentParser(
        description="Add CGI articles to the modification index",
    )

    parser.add_argument(
        "article",
        nargs="?",
        help="CGI article reference (e.g., '787 B')",
    )
    parser.add_argument(
        "-t", "--template",
        metavar="ARTICLE",
        help="Show JSON template for article",
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="List articles in index",
    )

    args = parser.parse_args()

    if args.list:
        index = load_index()
        articles = list(index.get("articles", {}).keys())
        print(f"\nArticles in index ({len(articles)}):")
        for ref in articles:
            info = index["articles"][ref]
            mods = len(info.get("modifications", []))
            print(f"  {ref}: {mods} modification(s)")
        return 0

    if args.template:
        show_template(args.template)
        return 0

    if args.article:
        interactive_add(args.article)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
