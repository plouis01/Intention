"""Légifrance API collector using PISTE OAuth authentication."""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from src.utils.config import get_settings

logger = logging.getLogger(__name__)


class LegifranceClient:
    """Client for the Légifrance API via PISTE platform."""

    def __init__(self):
        self.settings = get_settings()
        self._token: str | None = None
        self._token_expires: datetime | None = None
        self._client = httpx.Client(timeout=30.0)

    def _get_token(self) -> str:
        """Get or refresh OAuth token."""
        if self._token and self._token_expires and datetime.now() < self._token_expires:
            return self._token

        if not self.settings.piste_client_id or not self.settings.piste_client_secret:
            raise ValueError(
                "PISTE credentials not configured. "
                "Set PISTE_CLIENT_ID and PISTE_CLIENT_SECRET environment variables."
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
        # Expire 5 minutes early to be safe
        expires_in = data.get("expires_in", 3600) - 300
        self._token_expires = datetime.now() + timedelta(seconds=expires_in)

        logger.info("Obtained new Légifrance API token")
        return self._token

    def _request(self, method: str, endpoint: str, **kwargs) -> dict[str, Any]:
        """Make authenticated request to Légifrance API."""
        token = self._get_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"

        url = f"{self.settings.piste_api_base}{endpoint}"
        response = self._client.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()

        return response.json()

    def get_law_by_text_id(self, text_id: str) -> dict[str, Any]:
        """
        Fetch a law by its text ID (e.g., JORFTEXT000048727345).

        Args:
            text_id: The JORF text identifier

        Returns:
            Law data including articles
        """
        # Use the consult/jorf endpoint with textCid parameter
        response = self._request(
            "POST",
            "/consult/jorf",
            json={"textCid": text_id},
        )
        return response

    def get_article(self, article_id: str) -> dict[str, Any]:
        """
        Fetch a specific article by its ID.

        Args:
            article_id: The article identifier (LEGIARTI...)

        Returns:
            Article data
        """
        response = self._request(
            "POST",
            "/consult/getArticle",
            json={"id": article_id},
        )
        return response

    def search_laws(
        self,
        text_type: str = "LOI",
        date_start: str | None = None,
        date_end: str | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Search for laws by type and date range.

        Args:
            text_type: Type of text (LOI, ORDONNANCE, etc.)
            date_start: Start date in YYYY-MM-DD format
            date_end: End date in YYYY-MM-DD format
            page_size: Number of results per page

        Returns:
            List of matching laws
        """
        filters = {"textType": [text_type]}
        if date_start:
            filters["datePublication"] = {"start": date_start}
        if date_end:
            filters["datePublication"] = filters.get("datePublication", {})
            filters["datePublication"]["end"] = date_end

        response = self._request(
            "POST",
            "/search",
            json={
                "fond": "JORF",
                "recherche": {
                    "pageSize": page_size,
                    "sort": "DATE_DESC",
                    "filtres": [filters],
                },
            },
        )
        return response.get("results", [])

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def collect_law_texts(laws_file: Path, output_dir: Path) -> None:
    """
    Collect law texts from Légifrance for all laws in the reference file.

    Args:
        laws_file: Path to the laws reference JSON file
        output_dir: Directory to save collected law data
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(laws_file) as f:
        data = json.load(f)

    with LegifranceClient() as client:
        for loi in data["lois"]:
            jorf_id = loi.get("jorf")
            if not jorf_id:
                logger.warning(f"No JORF ID for law {loi['numero']}, skipping")
                continue

            output_file = output_dir / f"{jorf_id}.json"
            if output_file.exists():
                logger.info(f"Already collected {jorf_id}, skipping")
                continue

            try:
                logger.info(f"Fetching {loi['titre']} ({jorf_id})")
                law_data = client.get_law_by_text_id(jorf_id)

                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(law_data, f, ensure_ascii=False, indent=2)

                logger.info(f"Saved {jorf_id}")

            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error fetching {jorf_id}: {e}")
            except Exception as e:
                logger.error(f"Error fetching {jorf_id}: {e}")


def parse_law_articles(law_data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Parse articles from Légifrance law response.

    Args:
        law_data: Raw response from Légifrance API

    Returns:
        List of parsed articles with numero, contenu, position
    """
    articles = []

    # Navigate the nested structure - this may need adjustment based on actual API response
    texte = law_data.get("text", law_data)
    if isinstance(texte, dict):
        # Handle different response formats
        articles_data = texte.get("articles", [])
        if not articles_data and "sections" in texte:
            # Articles may be nested in sections
            for section in texte.get("sections", []):
                articles_data.extend(section.get("articles", []))

        for idx, art in enumerate(articles_data):
            articles.append({
                "numero_article": art.get("num", str(idx + 1)),
                "contenu_texte": art.get("content", art.get("texte", "")),
                "position_dans_loi": idx + 1,
                "id_legifrance": art.get("id"),
            })

    return articles


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    laws_file = Path("data/lois_finances_2012_2025.json")
    output_dir = Path("data/raw/legifrance")

    if not laws_file.exists():
        print(f"Laws file not found: {laws_file}")
    else:
        collect_law_texts(laws_file, output_dir)
