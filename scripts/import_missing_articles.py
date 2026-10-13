#!/usr/bin/env python3
"""Import articles for laws that are missing them."""

import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Article, Base, Loi

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_session():
    engine = create_engine("sqlite:///data/legislateur.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def extract_articles_from_legifrance_json(json_data: dict) -> list[dict]:
    """Extract articles from nested Légifrance JSON structure."""
    articles = []
    position = 0

    def extract_from_sections(sections: list, path_titles: list = None):
        nonlocal position
        path_titles = path_titles or []

        for section in sections:
            section_title = section.get("title", "")
            current_path = path_titles + [section_title] if section_title else path_titles

            # Extract articles in this section
            for article in section.get("articles", []):
                num = article.get("num")
                content = article.get("content", "")

                # Clean HTML from content
                if content:
                    content = re.sub(r"<[^>]+>", " ", content)
                    content = re.sub(r"\s+", " ", content).strip()

                if num and content:
                    position += 1
                    articles.append({
                        "numero_article": str(num),
                        "contenu_texte": content,
                        "position_dans_loi": position,
                    })

            # Recurse into nested sections
            if section.get("sections"):
                extract_from_sections(section["sections"], current_path)

    # Start extraction from top-level sections
    if "sections" in json_data:
        extract_from_sections(json_data["sections"])

    return articles


def main():
    session = get_session()
    legifrance_dir = Path("data/raw/legifrance")

    # Find laws without articles
    laws_without_articles = (
        session.query(Loi)
        .outerjoin(Article)
        .filter(Article.id.is_(None))
        .all()
    )

    logger.info(f"Found {len(laws_without_articles)} laws without articles")

    total_imported = 0

    for loi in laws_without_articles:
        # Extract JORF ID from URL
        jorf_id = None
        if loi.url_legifrance:
            match = re.search(r"(JORFTEXT\d+)", loi.url_legifrance)
            if match:
                jorf_id = match.group(1)

        if not jorf_id:
            logger.warning(f"No JORF ID for {loi.numero}")
            continue

        json_file = legifrance_dir / f"{jorf_id}.json"
        if not json_file.exists():
            logger.warning(f"No file for {jorf_id} ({loi.numero})")
            continue

        # Load and extract
        with open(json_file, encoding="utf-8") as f:
            data = json.load(f)

        # Skip placeholders
        if data.get("source") == "placeholder":
            logger.info(f"Skipping placeholder {loi.numero}")
            continue

        articles_data = extract_articles_from_legifrance_json(data)

        if not articles_data:
            logger.warning(f"No articles found in {json_file}")
            continue

        # Import articles
        imported = 0
        for art_data in articles_data:
            # Check if exists
            existing = session.query(Article).filter_by(
                loi_id=loi.id,
                numero_article=art_data["numero_article"]
            ).first()

            if existing:
                continue

            article = Article(
                loi_id=loi.id,
                numero_article=art_data["numero_article"],
                contenu_texte=art_data["contenu_texte"],
                position_dans_loi=art_data.get("position_dans_loi"),
            )
            session.add(article)
            imported += 1

        session.commit()
        total_imported += imported
        logger.info(f"Imported {imported} articles for {loi.numero}")

    logger.info(f"\n--- Summary ---")
    logger.info(f"Total articles imported: {total_imported}")

    # Final count
    total_articles = session.query(Article).count()
    logger.info(f"Total articles in DB: {total_articles}")

    session.close()


if __name__ == "__main__":
    main()
