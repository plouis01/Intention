#!/usr/bin/env python3
"""Scrape article concordance tables from the Sénat website.

For each finance law (LF, LFR, LFSS), the Sénat publishes a "table de concordance"
mapping PLF article numbers (during navette) to final law article numbers.

URL pattern: https://www.senat.fr/dossier-legislatif/tc/tc_{dossier_ref}.html

Output: data/concordance_articles.json
"""

import json
import logging
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from src.db.models import get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "concordance_articles.json"

# Known URL corrections (DB has wrong dossier ref)
URL_CORRECTIONS = {
    "pjl22-096": "pjl22-113",  # LFR 2022 (2) - DB ref returns 404
}

RATE_LIMIT_SECONDS = 0.5


def _extract_dossier_ref(url_source: str) -> str | None:
    """Extract dossier reference from Sénat url_source.

    Example: 'https://www.senat.fr/dossier-legislatif/pjlf2024.html' -> 'pjlf2024'
    """
    if not url_source:
        return None
    match = re.search(r"/dossier-legislatif/([^/]+)\.html", url_source)
    if match:
        ref = match.group(1)
        return URL_CORRECTIONS.get(ref, ref)
    return None


def _build_concordance_url(dossier_ref: str) -> str:
    """Build concordance table URL from dossier reference."""
    return f"https://www.senat.fr/dossier-legislatif/tc/tc_{dossier_ref}.html"


def _normalize_article_text(text_val: str) -> str | None:
    """Normalize article text from concordance table cell.

    Handles italic markup artifacts, whitespace, and common patterns.
    Returns None for empty/suppressed articles.
    """
    if not text_val:
        return None

    text_val = text_val.strip()

    # Check for suppressed articles
    if re.match(r"supprim", text_val, re.IGNORECASE):
        return None

    # Normalize whitespace
    text_val = re.sub(r"\s+", " ", text_val).strip()

    # Remove "Article " prefix if present
    text_val = re.sub(r"^(article\s+)", "", text_val, flags=re.IGNORECASE)

    # Fix "1 er" -> "1er" (HTML <sup>er</sup> artifact)
    text_val = re.sub(r"(\d+)\s+(er|ère)\b", r"\1\2", text_val)

    return text_val if text_val else None


def _parse_concordance_page(html: str) -> list[dict] | None:
    """Parse a concordance table HTML page.

    Returns list of {navette, definitif} dicts, or None if parsing fails.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Find the concordance table - it's the main table with article mappings
    tables = soup.find_all("table")
    if not tables:
        return None

    # Use the largest table (the concordance table is the main one)
    main_table = max(tables, key=lambda t: len(t.find_all("tr")))

    rows = main_table.find_all("tr")
    if len(rows) < 2:  # Need at least header + 1 data row
        return None

    articles = []
    for row in rows[1:]:  # Skip header
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        navette_text = cells[0].get_text(separator=" ", strip=True)
        definitif_text = cells[1].get_text(separator=" ", strip=True)

        navette = _normalize_article_text(navette_text)
        if not navette:
            continue  # Skip rows without a navette article

        definitif = _normalize_article_text(definitif_text)
        # definitif can be None (suppressed article)

        articles.append({
            "navette": navette,
            "definitif": definitif,
        })

    return articles if articles else None


def collect_concordance_tables():
    """Scrape concordance tables for all finance laws."""
    session = get_session()
    session.bind.echo = False

    # Get all laws with their Sénat dossier URLs
    results = session.execute(text("""
        SELECT l.numero, l.type, l.annee, dl.url_source
        FROM lois l
        JOIN dossiers_legislatifs dl ON dl.loi_id = l.id
        WHERE dl.chambre = 'SENAT'
        ORDER BY l.annee, l.type
    """)).fetchall()

    session.close()

    logger.info(f"Found {len(results)} law-dossier pairs")

    # Deduplicate by loi numero (keep first dossier per law)
    seen = set()
    laws = []
    for numero, loi_type, annee, url_source in results:
        if numero not in seen:
            seen.add(numero)
            laws.append((numero, loi_type, annee, url_source))

    logger.info(f"Processing {len(laws)} unique laws")

    concordance = {}
    errors = []

    for i, (numero, loi_type, annee, url_source) in enumerate(laws):
        dossier_ref = _extract_dossier_ref(url_source)
        if not dossier_ref:
            errors.append(f"{numero} ({loi_type} {annee}): no dossier ref from {url_source}")
            continue

        url = _build_concordance_url(dossier_ref)
        logger.info(f"[{i + 1}/{len(laws)}] {numero} ({loi_type} {annee}) -> {url}")

        try:
            resp = requests.get(url, timeout=30)

            # Fallback: for LF/LFSS, try canonical pjlf{YYYY}/plfss{YYYY} pattern
            if resp.status_code == 404:
                fallback_ref = None
                if loi_type == "LF":
                    fallback_ref = f"pjlf{annee}"
                elif loi_type == "LFSS":
                    fallback_ref = f"plfss{annee}"

                if fallback_ref and fallback_ref != dossier_ref:
                    fallback_url = _build_concordance_url(fallback_ref)
                    logger.info(f"  404, trying fallback: {fallback_url}")
                    time.sleep(RATE_LIMIT_SECONDS)
                    resp = requests.get(fallback_url, timeout=30)
                    if resp.status_code == 200:
                        url = fallback_url

            if resp.status_code == 404:
                errors.append(f"{numero}: 404 at {url}")
                continue
            resp.raise_for_status()

            articles = _parse_concordance_page(resp.text)
            if not articles:
                errors.append(f"{numero}: no articles parsed from {url}")
                continue

            concordance[numero] = {
                "loi_numero": numero,
                "loi_type": loi_type,
                "annee": annee,
                "url_source": url,
                "articles": articles,
            }

            suppressed = sum(1 for a in articles if a["definitif"] is None)
            logger.info(f"  -> {len(articles)} articles ({suppressed} suppressed)")

        except Exception as e:
            errors.append(f"{numero}: {e}")

        time.sleep(RATE_LIMIT_SECONDS)

    # Save output
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(concordance, f, ensure_ascii=False, indent=2)

    logger.info(f"\n--- Summary ---")
    logger.info(f"Laws processed: {len(laws)}")
    logger.info(f"Concordance tables saved: {len(concordance)}")
    logger.info(f"Errors: {len(errors)}")
    for err in errors:
        logger.warning(f"  {err}")
    logger.info(f"Output: {OUTPUT_PATH}")


if __name__ == "__main__":
    collect_concordance_tables()
