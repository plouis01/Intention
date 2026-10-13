#!/usr/bin/env python3
"""
Scrape CGI article modification history from Légifrance website.

This is a fallback method that scrapes the public Légifrance website
when the PISTE API is not available or not working.

Usage:
    python scripts/scrape_article_history.py "150-0 B ter"
    python scripts/scrape_article_history.py "787 B" --save
    python scripts/scrape_article_history.py --batch articles.txt --save

No API key required - uses public website.
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import httpx
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class LegifranceScraper:
    """Scraper for fetching CGI article history from Légifrance website."""

    BASE_URL = "https://www.legifrance.gouv.fr"
    SEARCH_URL = "https://www.legifrance.gouv.fr/search/code"

    # Common law type patterns
    LAW_PATTERNS = {
        r"loi\s+n[°o]?\s*(\d{4}-\d+).*finances\s+pour\s+(\d{4})": ("LF", "Loi de finances pour {year}"),
        r"loi\s+n[°o]?\s*(\d{4}-\d+).*finances\s+rectificative.*(\d{4})": ("LFR", "Loi de finances rectificative pour {year}"),
        r"loi\s+n[°o]?\s*(\d{4}-\d+).*financement.*sécurité\s+sociale.*(\d{4})": ("LFSS", "Loi de financement de la sécurité sociale pour {year}"),
        r"loi\s+n[°o]?\s*(\d{4}-\d+)": ("LOI", "Loi n°{numero}"),
        r"ordonnance\s+n[°o]?\s*(\d{4}-\d+)": ("ORD", "Ordonnance n°{numero}"),
        r"décret\s+n[°o]?\s*(\d{4}-\d+)": ("DEC", "Décret n°{numero}"),
    }

    def __init__(self):
        self._client = httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )

    def search_article(self, article_ref: str) -> str | None:
        """
        Search for a CGI article and return its URL.

        Args:
            article_ref: Article reference (e.g., "150-0 B ter")

        Returns:
            Article URL or None if not found
        """
        # Build search query
        search_query = f'"{article_ref}" code général impôts'
        params = {
            "tab_selection": "code",
            "searchField": "ALL",
            "query": search_query,
            "searchType": "ALL",
            "sortValue": "PERTINENCE",
            "pageSize": "10",
            "page": "1",
            "tab_selection": "code",
        }

        try:
            # Try direct CGI article URL pattern first
            # Format: /codes/article_lc/LEGIARTI...
            # We'll search instead
            response = self._client.get(
                self.SEARCH_URL,
                params=params,
            )
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            # Look for article links in results
            for link in soup.find_all("a", href=True):
                href = link.get("href", "")
                if "/codes/article_lc/" in href:
                    # Check if this is the right article
                    link_text = link.get_text(strip=True).lower()
                    ref_lower = article_ref.lower()
                    if ref_lower in link_text or ref_lower.replace(" ", "") in link_text.replace(" ", ""):
                        return urljoin(self.BASE_URL, href)

            # Try alternative search
            return self._search_via_code_toc(article_ref)

        except httpx.HTTPError as e:
            logger.error(f"Search failed: {e}")
            return None

    def _search_via_code_toc(self, article_ref: str) -> str | None:
        """Search for article via CGI table of contents."""
        # CGI code page
        cgi_url = "https://www.legifrance.gouv.fr/codes/id/LEGITEXT000006069577/"

        try:
            response = self._client.get(cgi_url)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            # Search for article in the page
            for link in soup.find_all("a", href=True):
                link_text = link.get_text(strip=True)
                if article_ref.lower() in link_text.lower():
                    href = link.get("href", "")
                    if "/codes/article_lc/" in href or "/codes/id/" in href:
                        return urljoin(self.BASE_URL, href)

        except httpx.HTTPError as e:
            logger.error(f"CGI TOC search failed: {e}")

        return None

    def get_article_page(self, article_url: str) -> dict | None:
        """
        Fetch and parse an article page.

        Args:
            article_url: URL of the article

        Returns:
            Dict with article info
        """
        try:
            response = self._client.get(article_url)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            # Extract article info
            article_info = {
                "url": article_url,
                "titre": "",
                "numero": "",
                "content": "",
                "versions": [],
            }

            # Title
            title_elem = soup.find("h1", class_="title-article") or soup.find("h1")
            if title_elem:
                article_info["titre"] = title_elem.get_text(strip=True)
                # Extract article number from title
                num_match = re.search(r"Article\s+(.+)", article_info["titre"], re.IGNORECASE)
                if num_match:
                    article_info["numero"] = num_match.group(1).strip()

            # Content
            content_elem = soup.find("div", class_="content-article") or soup.find("article")
            if content_elem:
                article_info["content"] = content_elem.get_text(strip=True)[:500]

            # Look for version history link
            versions_link = soup.find("a", href=re.compile(r"/codes/article_lc/.*#article"))
            if versions_link:
                article_info["versions_url"] = urljoin(self.BASE_URL, versions_link.get("href", ""))

            # Try to find modification history on the page
            history_section = soup.find("div", class_="historique") or soup.find(id="historique")
            if history_section:
                article_info["versions"] = self._parse_history_section(history_section)
            else:
                # Look for version dropdown or links
                article_info["versions"] = self._extract_versions_from_page(soup)

            return article_info

        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch article: {e}")
            return None

    def _parse_history_section(self, section) -> list[dict]:
        """Parse history section HTML."""
        versions = []

        for item in section.find_all("li"):
            version = {
                "date": "",
                "source": "",
                "nature": "Modification",
            }

            text = item.get_text(strip=True)

            # Extract date
            date_match = re.search(r"(\d{1,2})\s+(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+(\d{4})", text, re.IGNORECASE)
            if date_match:
                month_map = {
                    "janvier": "01", "février": "02", "mars": "03", "avril": "04",
                    "mai": "05", "juin": "06", "juillet": "07", "août": "08",
                    "septembre": "09", "octobre": "10", "novembre": "11", "décembre": "12",
                }
                day, month, year = date_match.groups()
                version["date"] = f"{year}-{month_map[month.lower()]}-{int(day):02d}"

            # Extract source law
            version["source"] = text
            versions.append(version)

        return versions

    def _extract_versions_from_page(self, soup) -> list[dict]:
        """Extract version info from article page."""
        versions = []

        # Look for "Modifié par" or "Créé par" sections
        for elem in soup.find_all(text=re.compile(r"(Modifié|Créé|Abrogé)\s+par", re.IGNORECASE)):
            parent = elem.parent
            if parent:
                text = parent.get_text(strip=True)
                version = self._parse_modification_text(text)
                if version:
                    versions.append(version)

        # Look in structured elements
        for link in soup.find_all("a", href=re.compile(r"/jorf/|/loda/")):
            text = link.get_text(strip=True)
            parent_text = link.parent.get_text(strip=True) if link.parent else text
            version = self._parse_modification_text(parent_text)
            if version and version not in versions:
                versions.append(version)

        return versions

    def _parse_modification_text(self, text: str) -> dict | None:
        """Parse modification text to extract law info."""
        if not text:
            return None

        # Determine nature
        if "créé par" in text.lower():
            nature = "Création"
        elif "abrogé par" in text.lower():
            nature = "Abrogation"
        else:
            nature = "Modification"

        # Extract law number
        law_match = re.search(r"n[°o]?\s*(\d{4}-\d+)", text)
        law_numero = law_match.group(1) if law_match else ""

        # Extract date
        date_match = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
        if date_match:
            day, month, year = date_match.groups()
            mod_date = f"{year}-{int(month):02d}-{int(day):02d}"
        else:
            # Try to extract year from law number
            year_match = re.search(r"(\d{4})-\d+", law_numero)
            if year_match:
                mod_date = f"{year_match.group(1)}-12-31"  # Default to end of year
            else:
                mod_date = ""

        if not law_numero and not mod_date:
            return None

        # Determine law type and title
        law_type, law_title = self._classify_law(text, law_numero)

        return {
            "date": mod_date,
            "law_numero": law_numero,
            "law_type": law_type,
            "law_year": int(mod_date[:4]) if mod_date else 0,
            "law_title": law_title,
            "article_in_law": "",
            "nature": nature,
            "legifrance_url": "",
            "source_text": text[:200],
        }

    def _classify_law(self, text: str, numero: str) -> tuple[str, str]:
        """Classify law type from text."""
        text_lower = text.lower()

        for pattern, (law_type, title_template) in self.LAW_PATTERNS.items():
            match = re.search(pattern, text_lower)
            if match:
                groups = match.groups()
                if len(groups) >= 2:
                    title = title_template.format(year=groups[1], numero=groups[0])
                else:
                    title = title_template.format(year="", numero=groups[0] if groups else numero)
                return law_type, title

        return "LOI", f"Loi n°{numero}" if numero else ""

    def fetch_article_history(self, article_ref: str) -> dict | None:
        """
        Fetch complete modification history for a CGI article.

        Args:
            article_ref: Article reference (e.g., "150-0 B ter")

        Returns:
            Dict with article info and modifications
        """
        logger.info(f"Searching for article {article_ref}...")

        # Search for article
        article_url = self.search_article(article_ref)
        if not article_url:
            logger.warning(f"Could not find article {article_ref}")
            return None

        logger.info(f"Found article at: {article_url}")

        # Small delay to be respectful
        time.sleep(1)

        # Get article page
        article_data = self.get_article_page(article_url)
        if not article_data:
            return None

        # Build result
        return {
            "reference": article_ref,
            "titre": article_data.get("titre", ""),
            "code": "CGI",
            "legifrance_url": article_url,
            "keywords": self._generate_keywords(article_ref, article_data),
            "modifications": article_data.get("versions", []),
            "jurisprudence": [],
            "synthese": {
                "objectif_constant": "",
                "preoccupations_recurrentes": [],
            },
        }

    def _generate_keywords(self, article_ref: str, article_data: dict) -> list[str]:
        """Generate keywords for the article."""
        keywords = [
            article_ref,
            article_ref.lower(),
            article_ref.replace("-", " "),
            article_ref.replace(" ", "").lower(),
        ]

        # Add from title
        titre = article_data.get("titre", "")
        for word in re.findall(r"[\w'-]+", titre.lower()):
            if len(word) > 4 and word not in ["article", "alinéa"]:
                keywords.append(word)

        return list(set(keywords))[:15]

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def save_to_index(article_data: dict, index_path: Path) -> None:
    """Save article data to the article_modifications.json index."""
    if index_path.exists():
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
    else:
        index = {"articles": {}}

    ref = article_data.pop("reference")

    # Clean up modifications
    modifications = article_data.get("modifications", [])
    for mod in modifications:
        mod.pop("source_text", None)

    index["articles"][ref] = article_data

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    logger.info(f"Saved {ref} to {index_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Scrape CGI article modification history from Légifrance website",
    )

    parser.add_argument(
        "article",
        nargs="?",
        help="CGI article reference (e.g., '150-0 B ter')",
    )
    parser.add_argument(
        "-s", "--save",
        action="store_true",
        help="Save to data/article_modifications.json",
    )
    parser.add_argument(
        "-b", "--batch",
        type=Path,
        help="File with article references (one per line)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Check for BeautifulSoup
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("ERROR: BeautifulSoup not installed.")
        print("Run: pip install beautifulsoup4")
        return 1

    # Get articles to process
    articles = []
    if args.batch:
        with open(args.batch) as f:
            articles = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    elif args.article:
        articles = [args.article]
    else:
        parser.print_help()
        return 1

    index_path = project_root / "data" / "article_modifications.json"

    with LegifranceScraper() as scraper:
        for article_ref in articles:
            print(f"\n{'='*60}")
            print(f"Processing: {article_ref}")
            print("=" * 60)

            try:
                article_data = scraper.fetch_article_history(article_ref)

                if not article_data:
                    print(f"ERROR: Could not fetch data for {article_ref}")
                    continue

                mods = article_data.get("modifications", [])
                print(f"Found {len(mods)} modification(s)")

                if args.save:
                    save_to_index(article_data.copy(), index_path)
                else:
                    print("\n" + json.dumps(article_data, ensure_ascii=False, indent=2))

                # Be respectful between requests
                if len(articles) > 1:
                    time.sleep(2)

            except Exception as e:
                print(f"ERROR: {e}")
                if args.verbose:
                    import traceback
                    traceback.print_exc()

    return 0


if __name__ == "__main__":
    sys.exit(main())
