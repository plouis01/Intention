#!/usr/bin/env python3
"""
Fetch CGI article modification history from Légifrance API.

This script queries the Légifrance PISTE API to get the versioning history
of CGI articles and generates entries for article_modifications.json.

Usage:
    python scripts/fetch_article_history.py "150-0 B ter"
    python scripts/fetch_article_history.py "787 B" --save
    python scripts/fetch_article_history.py --batch articles.txt --save

Requires:
    PISTE_CLIENT_ID and PISTE_CLIENT_SECRET environment variables
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import httpx

from src.utils.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class LegifranceCGIClient:
    """Client for fetching CGI article history from Légifrance API."""

    # CGI code identifier on Légifrance
    CGI_CODE_ID = "LEGITEXT000006069577"

    def __init__(self):
        self.settings = get_settings()
        self._token: str | None = None
        self._token_expires: datetime | None = None
        self._client = httpx.Client(timeout=60.0)

    def _get_token(self) -> str:
        """Get or refresh OAuth token."""
        from datetime import timedelta

        if self._token and self._token_expires and datetime.now() < self._token_expires:
            return self._token

        if not self.settings.piste_client_id or not self.settings.piste_client_secret:
            raise ValueError(
                "PISTE credentials not configured. "
                "Set PISTE_CLIENT_ID and PISTE_CLIENT_SECRET in .env"
            )

        response = self._client.post(
            self.settings.piste_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.settings.piste_client_id,
                "client_secret": self.settings.piste_client_secret,
                "scope": "openid",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()

        data = response.json()
        self._token = data["access_token"]
        expires_in = data.get("expires_in", 3600) - 300
        self._token_expires = datetime.now() + timedelta(seconds=expires_in)

        logger.info("Obtained Légifrance API token")
        return self._token

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Make authenticated request to Légifrance API."""
        token = self._get_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        headers["Content-Type"] = "application/json"

        url = f"{self.settings.piste_api_base}{endpoint}"
        logger.debug(f"Request: {method} {url}")

        response = self._client.request(method, url, headers=headers, **kwargs)

        if response.status_code != 200:
            logger.error(f"API error: {response.status_code} - {response.text[:500]}")

        response.raise_for_status()
        return response.json()

    def search_article_in_code(self, article_ref: str) -> dict | None:
        """
        Search for an article in the CGI by its reference.

        Args:
            article_ref: Article reference (e.g., "150-0 B ter", "787 B")

        Returns:
            Article data or None if not found
        """
        # Try multiple search strategies
        strategies = [
            self._search_code_date,
            self._search_all_fonds,
            self._search_code_etat,
        ]

        for strategy in strategies:
            try:
                result = strategy(article_ref)
                if result:
                    return result
            except httpx.HTTPStatusError as e:
                logger.debug(f"Strategy failed: {e}")
                continue

        logger.warning(f"No results found for {article_ref} with any strategy")
        return None

    def _search_code_date(self, article_ref: str) -> dict | None:
        """Search using CODE_DATE fond."""
        response = self._request(
            "POST",
            "/search",
            json={
                "fond": "CODE_DATE",
                "recherche": {
                    "champs": [
                        {
                            "typeChamp": "NUM_ARTICLE",
                            "criteres": [
                                {
                                    "typeRecherche": "EXACTE",
                                    "valeur": article_ref,
                                }
                            ],
                        }
                    ],
                    "filtres": [
                        {
                            "facette": "CODE_NAME",
                            "valeurs": ["Code général des impôts"],
                        }
                    ],
                    "pageNumber": 1,
                    "pageSize": 10,
                },
            },
        )
        results = response.get("results", [])
        return results[0] if results else None

    def _search_all_fonds(self, article_ref: str) -> dict | None:
        """Search using ALL fond with code filter."""
        response = self._request(
            "POST",
            "/search",
            json={
                "fond": "ALL",
                "recherche": {
                    "champs": [
                        {
                            "typeChamp": "ALL",
                            "criteres": [
                                {
                                    "typeRecherche": "EXACTE",
                                    "valeur": f"Article {article_ref}",
                                }
                            ],
                        }
                    ],
                    "filtres": [
                        {
                            "facette": "NATURE",
                            "valeurs": ["CODE"],
                        }
                    ],
                    "pageNumber": 1,
                    "pageSize": 10,
                },
            },
        )
        results = response.get("results", [])
        for result in results:
            # Check if it's from CGI
            if "impôt" in result.get("titre", "").lower() or "CGI" in result.get("titre", ""):
                return result
        return results[0] if results else None

    def _search_code_etat(self, article_ref: str) -> dict | None:
        """Search using CODE_ETAT fond."""
        response = self._request(
            "POST",
            "/search",
            json={
                "fond": "CODE_ETAT",
                "recherche": {
                    "champs": [
                        {
                            "typeChamp": "NUM_ARTICLE",
                            "criteres": [
                                {
                                    "typeRecherche": "CONTIENT",
                                    "valeur": article_ref,
                                }
                            ],
                        }
                    ],
                    "pageNumber": 1,
                    "pageSize": 10,
                },
            },
        )
        results = response.get("results", [])
        return results[0] if results else None

    def get_article_by_id(self, article_id: str) -> dict | None:
        """
        Get article details by its Légifrance ID.

        Args:
            article_id: Légifrance article ID (LEGIARTI...)

        Returns:
            Article data with versions
        """
        try:
            response = self._request(
                "POST",
                "/consult/getArticle",
                json={"id": article_id},
            )
            return response
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to get article {article_id}: {e}")
            return None

    def get_article_versions(self, article_id: str) -> list[dict]:
        """
        Get all versions of an article.

        Args:
            article_id: Légifrance article ID (LEGIARTI...)

        Returns:
            List of version data
        """
        try:
            response = self._request(
                "POST",
                "/consult/getArticleWithAncestorsAndChildren",
                json={"id": article_id},
            )

            # The API returns the article with its version history
            versions = []

            # Current article
            article = response.get("article", {})
            if article:
                versions.append(article)

            # Historical versions (ancestors)
            ancestors = response.get("ancestors", [])
            versions.extend(ancestors)

            return versions

        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to get versions for {article_id}: {e}")
            return []

    def get_article_chronology(self, article_cid: str) -> list[dict]:
        """
        Get the chronological list of all versions of an article.

        Args:
            article_cid: Article CID (common ID across versions)

        Returns:
            List of modifications with dates and source laws
        """
        try:
            response = self._request(
                "POST",
                "/list/getArticleLegalHistory",
                json={"textCid": article_cid},
            )
            return response.get("legalHistory", [])
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to get chronology for {article_cid}: {e}")
            return []

    def fetch_article_history(self, article_ref: str) -> dict | None:
        """
        Fetch complete modification history for a CGI article.

        Args:
            article_ref: Article reference (e.g., "150-0 B ter")

        Returns:
            Dict with article info and modifications, or None if not found
        """
        logger.info(f"Fetching history for article {article_ref}...")

        # Step 1: Search for the article
        search_result = self.search_article_in_code(article_ref)
        if not search_result:
            return None

        article_id = search_result.get("id")
        article_cid = search_result.get("cid")

        if not article_id:
            logger.error("No article ID in search result")
            return None

        logger.info(f"Found article ID: {article_id}, CID: {article_cid}")

        # Step 2: Get article details
        article_data = self.get_article_by_id(article_id)
        if not article_data:
            return None

        # Step 3: Get version history
        versions = self.get_article_versions(article_id)
        logger.info(f"Found {len(versions)} version(s)")

        # Step 4: Parse into modifications format
        modifications = self._parse_modifications(versions)

        # Build result
        article = article_data.get("article", {})
        return {
            "reference": article_ref,
            "titre": article.get("titre", ""),
            "code": "CGI",
            "legifrance_url": f"https://www.legifrance.gouv.fr/codes/article_lc/{article_id}",
            "article_id": article_id,
            "article_cid": article_cid,
            "keywords": self._extract_keywords(article_ref, article),
            "modifications": modifications,
        }

    def _parse_modifications(self, versions: list[dict]) -> list[dict]:
        """Parse version data into modifications format."""
        modifications = []

        for version in versions:
            # Extract modification info
            date_debut = version.get("dateDebut")
            if not date_debut:
                continue

            # Parse date (format: YYYY-MM-DD or timestamp)
            if isinstance(date_debut, int):
                # Timestamp in milliseconds
                mod_date = datetime.fromtimestamp(date_debut / 1000).strftime("%Y-%m-%d")
            else:
                mod_date = date_debut[:10]  # YYYY-MM-DD

            # Get source law info from liens
            liens = version.get("liens", version.get("lienModification", []))
            law_info = self._extract_law_info(liens, version)

            # Determine nature
            if version.get("etat") == "VIGUEUR_DIFF":
                nature = "Création"
            elif not modifications:  # First version we're processing
                nature = "Création"
            else:
                nature = "Modification"

            modifications.append({
                "date": mod_date,
                "law_numero": law_info.get("numero", ""),
                "law_type": law_info.get("type", ""),
                "law_year": law_info.get("year", 0),
                "law_title": law_info.get("title", ""),
                "article_in_law": law_info.get("article", ""),
                "nature": nature,
                "legifrance_url": law_info.get("url", ""),
            })

        # Sort by date
        modifications.sort(key=lambda x: x["date"])

        # Fix nature: first one should be Création
        if modifications:
            modifications[0]["nature"] = "Création"
            for mod in modifications[1:]:
                if mod["nature"] == "Création":
                    mod["nature"] = "Modification"

        return modifications

    def _extract_law_info(self, liens: list, version: dict) -> dict:
        """Extract law information from article links."""
        law_info = {
            "numero": "",
            "type": "",
            "year": 0,
            "title": "",
            "article": "",
            "url": "",
        }

        # Look for modification source in liens
        for lien in liens if isinstance(liens, list) else []:
            if lien.get("type") == "MODIFICATION" or lien.get("typeLien") == "MODIFIE_PAR":
                source = lien.get("textCid", lien.get("id", ""))

                # Try to extract law number from source
                # Pattern: n°YYYY-NNN
                numero_match = re.search(r"n[°o]?\s*(\d{4}-\d+)", lien.get("titre", ""))
                if numero_match:
                    law_info["numero"] = numero_match.group(1)
                    year_match = re.match(r"(\d{4})", law_info["numero"])
                    if year_match:
                        law_info["year"] = int(year_match.group(1))

                # Extract law type
                titre = lien.get("titre", "").lower()
                if "finances pour" in titre:
                    law_info["type"] = "LF"
                elif "finances rectificative" in titre:
                    law_info["type"] = "LFR"
                elif "financement de la sécurité sociale" in titre:
                    law_info["type"] = "LFSS"
                else:
                    law_info["type"] = "LOI"

                law_info["title"] = lien.get("titre", "")
                law_info["url"] = f"https://www.legifrance.gouv.fr/jorf/id/{source}" if source else ""

                # Use the ground-truth articleNum from the lien (mirrors
                # backfill_missing_modifications.build_modification_entry).
                # Regex on titre captured the wrong number on multi-article
                # titles and dropped suffixes like "Art. 39 C" → "Art. 39".
                article_num = lien.get("articleNum") or ""
                if article_num:
                    law_info["article"] = f"Art. {article_num}"

                break

        # Fallback: try to get from version's texteSource
        if not law_info["numero"]:
            texte_source = version.get("texteSource", {})
            if texte_source:
                law_info["numero"] = texte_source.get("numero", "")
                law_info["title"] = texte_source.get("titre", "")

        return law_info

    def _extract_keywords(self, article_ref: str, article: dict) -> list[str]:
        """Extract keywords from article content."""
        keywords = [article_ref]

        # Add common variants
        keywords.append(article_ref.replace("-", " "))
        keywords.append(article_ref.replace(" ", "").lower())

        # Extract from title/content
        titre = article.get("titre", "")
        if titre:
            # Extract key terms from title
            terms = re.findall(r"[\w'-]+", titre.lower())
            for term in terms:
                if len(term) > 4 and term not in ["article", "alinéa", "code"]:
                    keywords.append(term)

        return list(set(keywords))[:15]  # Limit to 15 keywords

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def save_to_index(article_data: dict, index_path: Path) -> None:
    """
    Save article data to the article_modifications.json index.

    Args:
        article_data: Article data to save
        index_path: Path to index file
    """
    # Load existing index
    if index_path.exists():
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
    else:
        index = {"articles": {}}

    # Add/update article
    ref = article_data.pop("reference")
    article_data.pop("article_id", None)  # Remove internal ID
    article_data.pop("article_cid", None)

    # Add empty jurisprudence and synthese if not present
    if "jurisprudence" not in article_data:
        article_data["jurisprudence"] = []
    if "synthese" not in article_data:
        article_data["synthese"] = {
            "objectif_constant": "",
            "preoccupations_recurrentes": [],
        }

    index["articles"][ref] = article_data

    # Save
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    logger.info(f"Saved {ref} to {index_path}")


def format_article_json(article_data: dict) -> str:
    """Format article data as pretty JSON for display."""
    return json.dumps(article_data, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch CGI article modification history from Légifrance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "150-0 B ter"           # Display history
  %(prog)s "787 B" --save          # Save to index
  %(prog)s --batch articles.txt    # Process multiple articles
        """,
    )

    parser.add_argument(
        "article",
        nargs="?",
        help="CGI article reference (e.g., '150-0 B ter', '787 B')",
    )
    parser.add_argument(
        "-s", "--save",
        action="store_true",
        help="Save to data/article_modifications.json",
    )
    parser.add_argument(
        "-b", "--batch",
        type=Path,
        help="File containing article references (one per line)",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        help="Output JSON file (for single article)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Get list of articles to process
    articles = []
    if args.batch:
        if not args.batch.exists():
            print(f"ERROR: Batch file not found: {args.batch}")
            return 1
        with open(args.batch) as f:
            articles = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    elif args.article:
        articles = [args.article]
    else:
        parser.print_help()
        return 1

    # Index path
    index_path = project_root / "data" / "article_modifications.json"

    # Process articles
    with LegifranceCGIClient() as client:
        for article_ref in articles:
            print(f"\n{'='*60}")
            print(f"Processing: {article_ref}")
            print("=" * 60)

            try:
                article_data = client.fetch_article_history(article_ref)

                if not article_data:
                    print(f"ERROR: Could not fetch data for {article_ref}")
                    continue

                # Display result
                print(f"\nFound {len(article_data.get('modifications', []))} modification(s)")

                if args.output and len(articles) == 1:
                    # Save to specified output file
                    with open(args.output, "w", encoding="utf-8") as f:
                        json.dump(article_data, f, ensure_ascii=False, indent=2)
                    print(f"Saved to {args.output}")

                elif args.save:
                    # Save to index
                    save_to_index(article_data.copy(), index_path)

                else:
                    # Display JSON
                    print("\n" + format_article_json(article_data))

            except Exception as e:
                print(f"ERROR: {e}")
                if args.verbose:
                    import traceback
                    traceback.print_exc()

    print(f"\nDone. Processed {len(articles)} article(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
