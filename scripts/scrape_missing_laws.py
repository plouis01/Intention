#!/usr/bin/env python3
"""Scrape missing law texts from Légifrance website."""

import json
import logging
import re
import sys
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Missing JORF IDs that returned 400 from API
MISSING_LAWS = [
    ("JORFTEXT000025517888", "Loi de finances rectificative pour 2012 (1)"),
    ("JORFTEXT000026288994", "Loi de finances rectificative pour 2012 (2)"),
    ("JORFTEXT000026856788", "Loi de finances pour 2013"),
    ("JORFTEXT000029988852", "Loi de finances pour 2015"),
    ("JORFTEXT000029988957", "Loi de finances rectificative pour 2014 (2)"),
    ("JORFTEXT000031732968", "Loi de finances rectificative pour 2015"),
    ("JORFTEXT000042556490", "Loi de finances rectificative pour 2020 (4)"),
    ("JORFTEXT000039682896", "Loi de financement de la sécurité sociale pour 2020"),
    ("JORFTEXT000028388125", "Loi de financement de la sécurité sociale pour 2014"),
]


def scrape_law_from_legifrance(jorf_id: str, client: httpx.Client) -> dict | None:
    """Scrape a law from Légifrance website."""
    url = f"https://www.legifrance.gouv.fr/jorf/id/{jorf_id}"

    try:
        response = client.get(url)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # Extract title
        title_elem = soup.find("h1")
        title = title_elem.get_text(strip=True) if title_elem else None

        # Extract articles
        articles = []
        article_divs = soup.find_all("div", class_="article-content")

        if not article_divs:
            # Try alternative structure
            article_divs = soup.find_all("article")

        for idx, div in enumerate(article_divs):
            # Get article number
            num_elem = div.find(class_="article-num") or div.find("h2")
            num = num_elem.get_text(strip=True) if num_elem else str(idx + 1)

            # Clean article number
            num = re.sub(r"^Article\s*", "", num, flags=re.IGNORECASE).strip()

            # Get content
            content_elem = div.find(class_="article-body") or div
            content = content_elem.get_text(separator="\n", strip=True)

            if content and len(content) > 10:
                articles.append({
                    "num": num,
                    "content": content,
                    "id": f"{jorf_id}_ART_{idx+1}",
                })

        if not articles:
            logger.warning(f"  No articles found in HTML for {jorf_id}")
            return None

        return {
            "text": {
                "id": jorf_id,
                "title": title,
                "articles": articles,
            },
            "source": "legifrance_scrape",
        }

    except httpx.HTTPStatusError as e:
        logger.error(f"  HTTP error: {e}")
        return None
    except Exception as e:
        logger.error(f"  Error scraping: {e}")
        return None


def main():
    output_dir = Path("data/raw/legifrance")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check which ones already exist
    existing = {p.stem for p in output_dir.glob("*.json")}
    to_fetch = [(jorf, titre) for jorf, titre in MISSING_LAWS if jorf not in existing]

    if not to_fetch:
        logger.info("All laws already fetched!")
        return

    logger.info(f"Need to scrape {len(to_fetch)} laws from website")

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }

    with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers) as client:
        for i, (jorf_id, titre) in enumerate(to_fetch):
            output_file = output_dir / f"{jorf_id}.json"

            logger.info(f"[{i+1}/{len(to_fetch)}] Scraping {titre} ({jorf_id})")
            law_data = scrape_law_from_legifrance(jorf_id, client)

            if law_data:
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(law_data, f, ensure_ascii=False, indent=2)
                logger.info(f"  Saved {jorf_id} with {len(law_data['text']['articles'])} articles")
            else:
                logger.warning(f"  Could not scrape {jorf_id}")

            # Wait between requests
            if i < len(to_fetch) - 1:
                time.sleep(2)

    # Final summary
    final_existing = {p.stem for p in output_dir.glob("*.json")}
    still_missing = [jorf for jorf, _ in MISSING_LAWS if jorf not in final_existing]

    logger.info(f"\n--- Summary ---")
    logger.info(f"Total Légifrance files: {len(final_existing)}")
    logger.info(f"Still missing: {len(still_missing)}")


if __name__ == "__main__":
    main()
