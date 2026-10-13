"""Article modification index loader."""

import json
import logging
from datetime import datetime
from pathlib import Path

from src.analysis.models import (
    ArticleInfo,
    JurisprudenceEntry,
    LawModification,
    ModificationNature,
)

logger = logging.getLogger(__name__)

# Default path to the article modifications index
DEFAULT_INDEX_PATH = Path(__file__).parent.parent.parent / "data" / "article_modifications.json"


class ArticleIndex:
    """Index of article modifications for lookup."""

    def __init__(self, index_path: Path | None = None):
        self.index_path = index_path or DEFAULT_INDEX_PATH
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        """Load the index from JSON file."""
        if not self.index_path.exists():
            logger.warning(f"Article index not found at {self.index_path}")
            return

        with open(self.index_path, encoding="utf-8") as f:
            self._data = json.load(f)

        logger.info(f"Loaded article index with {len(self._data.get('articles', {}))} articles")

    def get_article(self, article_ref: str) -> ArticleInfo | None:
        """
        Get article information by reference.

        Args:
            article_ref: Article reference (e.g., "150-0 B ter")

        Returns:
            ArticleInfo or None if not found
        """
        articles = self._data.get("articles", {})

        # Try exact match first
        if article_ref in articles:
            return self._parse_article(article_ref, articles[article_ref])

        # Try normalized match
        normalized = article_ref.replace(" ", "").lower()
        for ref, data in articles.items():
            if ref.replace(" ", "").lower() == normalized:
                return self._parse_article(ref, data)

        return None

    def _resolve_ref(self, article_ref: str) -> str | None:
        """Resolve article_ref to the canonical key via case-insensitive, space-normalised lookup.

        Returns the canonical key string, or None if not found.
        """
        articles = self._data.get("articles", {})
        if article_ref in articles:
            return article_ref
        normalized = article_ref.replace(" ", "").lower()
        for ref in articles:
            if ref.replace(" ", "").lower() == normalized:
                return ref
        return None

    def _parse_article(self, ref: str, data: dict) -> ArticleInfo:
        """Parse article data into ArticleInfo object."""
        modifications = []
        for mod in data.get("modifications", []):
            modifications.append(
                LawModification(
                    date=datetime.strptime(mod["date"], "%Y-%m-%d").date(),
                    law_numero=mod["law_numero"],
                    law_type=mod["law_type"],
                    law_year=mod["law_year"],
                    article_in_law=mod["article_in_law"],
                    nature=ModificationNature(mod["nature"]),
                    legifrance_url=mod.get("legifrance_url", ""),
                    law_title=mod.get("law_title", ""),
                )
            )

        return ArticleInfo(
            reference=ref,
            titre=data.get("titre", ""),
            code=data.get("code", "CGI"),
            keywords=data.get("keywords", []),
            modifications=modifications,
            legifrance_url=data.get("legifrance_url", ""),
            bofip_url=data.get("bofip_url", ""),
        )

    def get_jurisprudence(self, article_ref: str) -> list[JurisprudenceEntry]:
        """Get jurisprudence entries for an article."""
        articles = self._data.get("articles", {})
        key = self._resolve_ref(article_ref)
        data = articles.get(key, {}) if key else {}

        entries = []
        for j in data.get("jurisprudence", []):
            entries.append(
                JurisprudenceEntry(
                    juridiction=j.get("juridiction", ""),
                    date=j.get("date", ""),
                    reference=j.get("reference", ""),
                    titre=j.get("titre", ""),
                    faits=j.get("faits", ""),
                    solution=j.get("solution", ""),
                    portee=j.get("portee", ""),
                )
            )

        return entries

    def get_synthese(self, article_ref: str) -> dict:
        """Get synthesis data for an article."""
        articles = self._data.get("articles", {})
        key = self._resolve_ref(article_ref)
        data = articles.get(key, {}) if key else {}
        return data.get("synthese", {})

    def get_synthese_evolution(self, article_ref: str) -> list[dict]:
        """Get predefined synthesis evolution data for an article."""
        articles = self._data.get("articles", {})
        key = self._resolve_ref(article_ref)
        data = articles.get(key, {}) if key else {}
        return data.get("synthese_evolution", [])

    def get_curated_quotes(self, article_ref: str, year: str) -> list[dict]:
        """Get curated quotes for a specific modification year."""
        articles = self._data.get("articles", {})
        key = self._resolve_ref(article_ref)
        data = articles.get(key, {}) if key else {}
        curated = data.get("curated_quotes", {})
        return curated.get(year, [])

    def get_forward_looking(self, article_ref: str) -> list[dict]:
        """Get forward-looking PLF discussion data."""
        articles = self._data.get("articles", {})
        key = self._resolve_ref(article_ref)
        data = articles.get(key, {}) if key else {}
        return data.get("forward_looking", [])

    def get_rapports_parlementaires(self, article_ref: str) -> list[dict]:
        """Get parliamentary report references."""
        articles = self._data.get("articles", {})
        key = self._resolve_ref(article_ref)
        data = articles.get(key, {}) if key else {}
        return data.get("rapports_parlementaires", [])

    def get_dila_sources(self, article_ref: str) -> list[str]:
        """Get DILA source references."""
        articles = self._data.get("articles", {})
        key = self._resolve_ref(article_ref)
        data = articles.get(key, {}) if key else {}
        return data.get("dila_sources", [])

    def list_articles(self) -> list[str]:
        """List all article references in the index."""
        return list(self._data.get("articles", {}).keys())


# Global instance
_index: ArticleIndex | None = None


def get_article_index() -> ArticleIndex:
    """Get the global article index instance."""
    global _index
    if _index is None:
        _index = ArticleIndex()
    return _index
