"""Article modification index loader."""

import json
import logging
from datetime import datetime
from pathlib import Path

from src.analysis.models import (
    ArticleInfo,
    LawModification,
    ModificationNature,
)

logger = logging.getLogger(__name__)

# Default path to the article modifications index
DEFAULT_INDEX_PATH = Path(__file__).parent.parent.parent / "data" / "modifications_cgi_enriched.json"


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
            # Skip modifications with no date (INCONNU historical entries)
            mod_date_str = mod.get("date", "")
            if not mod_date_str:
                continue
            try:
                mod_date = datetime.strptime(mod_date_str, "%Y-%m-%d").date()
            except ValueError:
                logger.warning(f"Invalid date '{mod_date_str}' for {ref}, skipping")
                continue
            modifications.append(
                LawModification(
                    date=mod_date,
                    law_numero=mod.get("law_numero", ""),
                    law_type=mod.get("law_type", "INCONNU"),
                    law_year=mod.get("law_year") or 0,
                    article_in_law=mod.get("article_in_law") or "",
                    nature=ModificationNature(mod["nature"]),
                    legifrance_url=mod.get("legifrance_url", ""),
                    law_title=mod.get("law_title", ""),
                    objet=mod.get("objet", ""),
                    date_debut_vigueur=mod.get("date_debut_vigueur", ""),
                    date_fin_vigueur=mod.get("date_fin_vigueur", ""),
                    nor=mod.get("nor", ""),
                )
            )

        return ArticleInfo(
            reference=ref,
            titre=data.get("titre", ""),
            code=data.get("code", "CGI"),
            keywords=data.get("keywords", []),
            modifications=modifications,
            legifrance_url=data.get("legifrance_url", ""),
            etat_actuel=data.get("etat_actuel", ""),
            texte_actuel=data.get("texte_actuel", ""),
            nota=data.get("nota", ""),
            hierarchie=data.get("hierarchie", []),
            article_ids=data.get("article_ids", []),
            references_croisees=data.get("references_croisees", []),
        )

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
