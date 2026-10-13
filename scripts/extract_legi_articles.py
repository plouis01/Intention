"""Extract articles from DILA LEGI dump for pre-2002 finance laws.

This script reads the Freemium_legi_global tar.gz and extracts article content
for laws that have empty articles in the Legifrance JSON files.

It produces enriched JSON files compatible with the existing import pipeline.
"""

import json
import logging
import re
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
LEGI_ARCHIVE = DATA_DIR / "raw/legi/Freemium_legi_global.tar.gz"
LEGIFRANCE_DIR = DATA_DIR / "raw/legifrance"
REFERENCE_FILE = DATA_DIR / "lois_cgi_1958_2026.json"

FINANCE_TYPES = {"LF", "LFR", "LFSS"}


def load_target_jorf_ids(finance_only: bool = True) -> dict[str, dict]:
    """Load JORF IDs of laws that need article content.

    Args:
        finance_only: If True, only target LF/LFR/LFSS. If False, target all types.
    """
    with open(REFERENCE_FILE) as f:
        data = json.load(f)

    targets = {}
    for loi in data["lois"]:
        if finance_only and loi["type"] not in FINANCE_TYPES:
            continue
        jorf = loi.get("jorf")
        if not jorf:
            continue

        # Check if existing JSON already has articles with content
        json_file = LEGIFRANCE_DIR / f"{jorf}.json"
        has_content = False
        if json_file.exists():
            with open(json_file) as f:
                jdata = json.load(f)
            has_content = _json_has_article_content(jdata)

        if not has_content:
            targets[jorf] = {
                "numero": loi["numero"],
                "type": loi["type"],
                "titre": loi.get("titre", ""),
                "date_promulgation": loi["date_promulgation"],
            }

    return targets


def _json_has_article_content(json_data: dict) -> bool:
    """Check if a Legifrance JSON file has articles with actual content."""
    def check_sections(sections):
        for section in sections:
            for article in section.get("articles", []):
                if article.get("num") and article.get("content", "").strip():
                    return True
            if check_sections(section.get("sections", [])):
                return True
        return False

    if check_sections(json_data.get("sections", [])):
        return True
    for article in json_data.get("articles", []):
        if article.get("num") and article.get("content", "").strip():
            return True
    return False


def parse_legi_article_xml(xml_content: bytes) -> dict | None:
    """Parse a LEGI ARTICLE XML and extract article data."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    # Get article number
    num_el = root.find(".//META_ARTICLE/NUM")
    num = num_el.text.strip() if num_el is not None and num_el.text else None

    # Get article state
    etat_el = root.find(".//META_ARTICLE/ETAT")
    etat = etat_el.text.strip() if etat_el is not None and etat_el.text else None

    # Get date range
    date_debut_el = root.find(".//META_ARTICLE/DATE_DEBUT")
    date_debut = date_debut_el.text.strip() if date_debut_el is not None and date_debut_el.text else None

    # Get content from BLOC_TEXTUEL/CONTENU
    bloc = root.find(".//BLOC_TEXTUEL/CONTENU")
    content_html = ""
    if bloc is not None:
        # Get all text content including child elements
        content_html = ET.tostring(bloc, encoding="unicode", method="html")
        # Remove the wrapping <CONTENU> tags
        content_html = re.sub(r"^<CONTENU>|</CONTENU>$", "", content_html, flags=re.IGNORECASE).strip()

    # Get NOTA
    nota_el = root.find(".//NOTA/CONTENU")
    nota = ""
    if nota_el is not None:
        nota = ET.tostring(nota_el, encoding="unicode", method="html")
        nota = re.sub(r"^<CONTENU>|</CONTENU>$", "", nota, flags=re.IGNORECASE).strip()

    # Get article ID
    id_el = root.find(".//META_COMMUN/ID")
    art_id = id_el.text.strip() if id_el is not None and id_el.text else None

    # Get context info for ordering
    int_ordre = None
    versions = root.findall(".//VERSIONS/VERSION")

    return {
        "id": art_id,
        "num": num,
        "etat": etat,
        "date_debut": date_debut,
        "content": content_html,
        "nota": nota,
    }


def extract_articles_for_laws(target_jorf_ids: dict[str, dict]) -> dict[str, list[dict]]:
    """Extract articles from LEGI dump for target laws.

    Returns dict mapping JORF ID -> list of article dicts.
    """
    results: dict[str, list[dict]] = {jorf: [] for jorf in target_jorf_ids}
    found_jorfs = set()

    logger.info(f"Opening LEGI archive: {LEGI_ARCHIVE}")
    logger.info(f"Looking for {len(target_jorf_ids)} laws...")

    with tarfile.open(LEGI_ARCHIVE, "r:gz") as tf:
        for member in tf:
            if not member.name.endswith(".xml"):
                continue

            # Check if this file belongs to one of our target laws
            matched_jorf = None
            for jorf_id in target_jorf_ids:
                if jorf_id in member.name:
                    matched_jorf = jorf_id
                    break

            if not matched_jorf:
                continue

            # Only process article files
            if "/article/" not in member.name:
                # But also grab the struct file for ordering
                if "/texte/struct/" in member.name:
                    found_jorfs.add(matched_jorf)
                continue

            f = tf.extractfile(member)
            if not f:
                continue

            xml_content = f.read()
            article = parse_legi_article_xml(xml_content)
            if article and article["content"]:
                results[matched_jorf].append(article)
                found_jorfs.add(matched_jorf)

    logger.info(f"Found articles for {len(found_jorfs)}/{len(target_jorf_ids)} laws")

    # Deduplicate: keep only the most recent version of each article number
    for jorf_id, articles in results.items():
        results[jorf_id] = _deduplicate_articles(articles)

    return results


def _deduplicate_articles(articles: list[dict]) -> list[dict]:
    """Keep only the initial/earliest version of each article number.

    For finance laws, we want the version as published (date closest to
    the law's publication), not later consolidated versions.
    """
    if not articles:
        return []

    by_num: dict[str, list[dict]] = {}
    no_num = []

    for art in articles:
        num = art.get("num")
        if num:
            by_num.setdefault(num, []).append(art)
        elif art.get("content"):
            no_num.append(art)

    deduped = []
    for num, versions in sorted(by_num.items(), key=_article_sort_key):
        # Sort by date_debut ascending, pick earliest version that has content
        versions.sort(key=lambda a: a.get("date_debut", "9999"))
        deduped.append(versions[0])

    # Add unnumbered articles at the end
    deduped.extend(no_num)

    return deduped


def _article_sort_key(item):
    """Sort articles by number, handling mixed numeric/alpha article nums."""
    num = item[0]
    # Try to extract leading number for sorting
    match = re.match(r"(\d+)", num)
    if match:
        return (0, int(match.group(1)), num)
    return (1, 0, num)


def update_legifrance_jsons(articles_by_jorf: dict[str, list[dict]], target_info: dict[str, dict]):
    """Update Legifrance JSON files with LEGI article content."""
    updated = 0
    created = 0

    for jorf_id, articles in articles_by_jorf.items():
        if not articles:
            continue

        json_file = LEGIFRANCE_DIR / f"{jorf_id}.json"
        info = target_info[jorf_id]

        if json_file.exists():
            # Update existing JSON with article content from LEGI
            with open(json_file) as f:
                data = json.load(f)

            # Add LEGI articles as a new field
            data["legi_articles"] = [
                {
                    "num": art["num"],
                    "content": art["content"],
                    "id": art["id"],
                    "etat": art["etat"],
                    "date_debut": art["date_debut"],
                    "nota": art.get("nota", ""),
                }
                for art in articles
            ]
            data["legi_source"] = True

            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            updated += 1
            logger.info(f"Updated {jorf_id} ({info['numero']}): {len(articles)} articles from LEGI")
        else:
            # Create new JSON from LEGI data
            data = {
                "title": info["titre"],
                "numero": info["numero"],
                "type": info["type"],
                "date_promulgation": info["date_promulgation"],
                "legi_source": True,
                "legi_articles": [
                    {
                        "num": art["num"],
                        "content": art["content"],
                        "id": art["id"],
                        "etat": art["etat"],
                        "date_debut": art["date_debut"],
                        "nota": art.get("nota", ""),
                    }
                    for art in articles
                ],
                "sections": [],
                "articles": [],
            }

            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            created += 1
            logger.info(f"Created {jorf_id} ({info['numero']}): {len(articles)} articles from LEGI")

    return updated, created


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-types", action="store_true",
                        help="Target all law types, not just LF/LFR/LFSS")
    args = parser.parse_args()

    if not LEGI_ARCHIVE.exists():
        logger.error(f"LEGI archive not found: {LEGI_ARCHIVE}")
        logger.error("Download from https://echanges.dila.gouv.fr/OPENDATA/LEGI/")
        return

    # Step 1: Identify laws needing article content
    logger.info("Loading target laws...")
    targets = load_target_jorf_ids(finance_only=not args.all_types)
    logger.info(f"Found {len(targets)} laws without article content")

    if not targets:
        logger.info("All laws already have article content!")
        return

    for jorf, info in sorted(targets.items(), key=lambda x: x[1]["date_promulgation"]):
        logger.info(f"  {info['numero']:20s} ({info['type']}) - {jorf}")

    # Step 2: Extract articles from LEGI dump
    articles_by_jorf = extract_articles_for_laws(targets)

    total_articles = sum(len(arts) for arts in articles_by_jorf.values())
    laws_with_articles = sum(1 for arts in articles_by_jorf.values() if arts)
    logger.info(f"\nExtracted {total_articles} articles for {laws_with_articles} laws")

    # Show stats per law
    for jorf_id in sorted(articles_by_jorf.keys(), key=lambda j: targets[j]["date_promulgation"]):
        arts = articles_by_jorf[jorf_id]
        if arts:
            logger.info(f"  {targets[jorf_id]['numero']:20s}: {len(arts)} articles")
        else:
            logger.warning(f"  {targets[jorf_id]['numero']:20s}: NO articles found in LEGI dump")

    # Step 3: Update JSON files
    logger.info("\nUpdating Legifrance JSON files...")
    updated, created = update_legifrance_jsons(articles_by_jorf, targets)
    logger.info(f"Done: {updated} updated, {created} created")


if __name__ == "__main__":
    main()
