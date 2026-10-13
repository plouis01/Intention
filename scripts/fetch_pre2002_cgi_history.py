#!/usr/bin/env python3
"""Fetch pre-2002 CGI article modification history from Légifrance API.

Queries the Légifrance PISTE API for each CGI article's lienModifications
(in deep mode: all historical versions too), filters to pre-2002, and
merges into article_modifications.json.

Usage:
    python scripts/fetch_pre2002_cgi_history.py --max-articles 10 --dry-run
    python scripts/fetch_pre2002_cgi_history.py --resume
    python scripts/fetch_pre2002_cgi_history.py --workers 8

Requires:
    PISTE_CLIENT_ID and PISTE_CLIENT_SECRET in .env
"""

import argparse
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx

from scripts.collect_cgi_laws import (
    CGI_TEXT_ID,
    LegifranceCodeClient,
    _classify_law_type,
    _extract_date_from_titre,
    _extract_numero,
)
from scripts.generate_article_index import (
    build_article_index,
    enrich_from_reference,
    enrich_law_titles,
    generate_article_keywords,
    load_law_titles,
)
from src.analysis.cgi_extractor import ExtractedModification
from src.db.models import get_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

TOC_CACHE_FILE = PROJECT_ROOT / "data" / "cgi_toc_cache.json"
INDEX_PATH = PROJECT_ROOT / "data" / "article_modifications.json"
REFERENCE_PATH = PROJECT_ROOT / "data" / "cgi_articles_reference.json"
CHECKPOINT_FILE = PROJECT_ROOT / "data" / "pre2002_history_checkpoint.json"
CUTOFF_DATE = "2002-01-01"


def collect_cgi_articles_fast(client: LegifranceCodeClient) -> list[dict]:
    """Collect all CGI article IDs from root TOC only (skip leaf drilling).

    Leaf drilling was tested and found no additional articles, so we skip it
    to save ~2 min of API calls.
    """
    # Check cache first
    if TOC_CACHE_FILE.exists():
        with open(TOC_CACHE_FILE, encoding="utf-8") as f:
            articles = json.load(f)
        logger.info(f"Loaded {len(articles)} articles from TOC cache")
        return articles

    logger.info("Fetching CGI root table of contents...")
    toc = client.get_code_toc(CGI_TEXT_ID)

    seen: set[str] = set()
    articles: list[dict] = []

    def _walk(node: dict) -> None:
        for entry in node.get("articles", []):
            aid = entry.get("id")
            if aid and aid not in seen:
                seen.add(aid)
                articles.append({
                    "id": aid,
                    "num": entry.get("num", ""),
                    "cid": entry.get("cid"),
                })
        for section in node.get("sections", []):
            _walk(section)

    _walk(toc)
    logger.info(f"Root tree: {len(articles)} articles (leaf drilling skipped)")

    # Cache for future runs
    TOC_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOC_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False)
    logger.info(f"TOC cached to {TOC_CACHE_FILE}")

    return articles


def _map_link_type_to_nature(link_type: str) -> str:
    """Map Légifrance linkType to modification nature."""
    lt = (link_type or "").upper()
    if "CREA" in lt or "CREE" in lt:
        return "Création"
    if "ABROG" in lt:
        return "Abrogation"
    if "MODIF" in lt:
        return "Modification"
    if "TRANSF" in lt:
        return "Transfert"
    if "PERIM" in lt:
        return "Péremption"
    return "Modification"


def extract_pre2002_from_article(
    client: LegifranceCodeClient,
    article: dict,
) -> list[dict]:
    """Extract pre-2002 modifications from a CGI article (deep mode).

    Returns list of dicts with keys: cgi_num, law_numero, law_type,
    date_promulgation, nature, titre.
    """
    article_id = article["id"]
    article_num = article.get("num", "")
    results = []
    seen = set()  # (law_numero, nature) dedup

    try:
        data = client.get_article(article_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 404, 400, 403):
            return []
        raise

    art_data = data.get("article")
    if not art_data or not isinstance(art_data, dict):
        return []

    def _process_liens(liens, source_label="current"):
        """Process lienModifications and extract pre-2002 modifications."""
        for lien in liens or []:
            link_type = lien.get("linkType", lien.get("typeLien", ""))
            nature = _map_link_type_to_nature(link_type)

            # Parse law info
            titre = lien.get("textTitle", "")
            if not titre:
                continue

            numero = _extract_numero(titre)
            num_texte = lien.get("numTexte") or ""
            if not numero:
                if num_texte and re.search(r"\d{4}-\d{3,}", num_texte):
                    numero = num_texte
                else:
                    continue

            # Get date
            date_pub = ""
            date_ms = lien.get("datePubli")
            if date_ms and isinstance(date_ms, (int, float)):
                try:
                    from datetime import datetime
                    date_pub = datetime.fromtimestamp(date_ms / 1000).strftime("%Y-%m-%d")
                except (OSError, ValueError):
                    pass
            if not date_pub:
                date_pub = _extract_date_from_titre(titre)
            if not date_pub:
                continue

            # Filter: pre-2002 only
            if date_pub >= CUTOFF_DATE:
                continue

            law_type = _classify_law_type(titre)

            dedup_key = (numero, nature)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            results.append({
                "cgi_num": article_num,
                "law_numero": numero,
                "law_type": law_type,
                "date_promulgation": date_pub,
                "nature": nature,
                "titre": titre.strip()[:300],
            })

    # Process current version's lienModifications
    _process_liens(art_data.get("lienModifications"))

    # Deep mode: fetch all historical versions
    versions = art_data.get("articleVersions", [])
    seen_ids = {article_id}
    for version in versions:
        vid = version.get("id")
        if not vid or vid in seen_ids:
            continue
        seen_ids.add(vid)
        try:
            vdata = client.get_article(vid)
        except (httpx.HTTPStatusError, httpx.HTTPError):
            continue
        vart = vdata.get("article")
        if not vart or not isinstance(vart, dict):
            continue
        _process_liens(vart.get("lienModifications"), source_label=vid)

    return results


def save_checkpoint(articles_done: list[str], raw_mods: list[dict]):
    """Save progress to checkpoint file."""
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "articles_done": articles_done,
            "raw_mods": raw_mods,
        }, f, ensure_ascii=False)
    logger.debug(f"Checkpoint: {len(articles_done)} articles, {len(raw_mods)} mods")


def load_checkpoint() -> tuple[list[str], list[dict]]:
    """Load progress from checkpoint."""
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        logger.info(
            f"Resuming: {len(data['articles_done'])} articles done, "
            f"{len(data['raw_mods'])} mods found"
        )
        return data["articles_done"], data["raw_mods"]
    return [], []


def convert_to_extracted_modifications(
    raw_mods: list[dict],
) -> dict[str, list[ExtractedModification]]:
    """Convert raw modification dicts to ExtractedModification grouped by CGI article."""
    by_article: dict[str, list[ExtractedModification]] = {}
    seen: dict[str, set] = {}  # article -> set of (law_numero, nature)

    for mod in raw_mods:
        cgi_num = mod["cgi_num"]
        if not cgi_num:
            continue

        # Dedup
        key = (mod["law_numero"], mod["nature"])
        if cgi_num not in seen:
            seen[cgi_num] = set()
        if key in seen[cgi_num]:
            continue
        seen[cgi_num].add(key)

        # Extract year from numero
        year_match = re.match(r"(\d{4})-", mod["law_numero"])
        if year_match:
            year = int(year_match.group(1))
        else:
            # Old format YY-NNN: derive from date
            dp = mod["date_promulgation"]
            year = int(dp[:4]) if dp and len(dp) >= 4 else 0

        em = ExtractedModification(
            cgi_article=cgi_num,
            law_numero=mod["law_numero"],
            law_type=mod["law_type"],
            law_year=year,
            article_in_law="(API)",  # From API, not from a specific article
            date_promulgation=mod["date_promulgation"],
            nature=mod["nature"],
            context=mod.get("titre", ""),
            confidence=1.0,  # Authoritative API source
        )

        if cgi_num not in by_article:
            by_article[cgi_num] = []
        by_article[cgi_num].append(em)

    return by_article


def main():
    # Force unbuffered output for background runs
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(
        description="Fetch pre-2002 CGI modification history from Légifrance API",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show stats without writing to index")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from checkpoint")
    parser.add_argument("--max-articles", type=int, default=0,
                        help="Limit articles to process (0=all)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel workers (default: 4)")
    parser.add_argument("--skip-toc", action="store_true",
                        help="Skip TOC fetch, use checkpoint articles only")
    args = parser.parse_args()

    # Load existing index
    if not INDEX_PATH.exists():
        logger.error(f"Index not found: {INDEX_PATH}")
        return 1
    with open(INDEX_PATH, encoding="utf-8") as f:
        existing_index = json.load(f)
    existing_articles = existing_index.get("articles", {})
    existing_mod_count = sum(len(a.get("modifications", [])) for a in existing_articles.values())
    post2002_count = sum(
        1 for a in existing_articles.values()
        for m in a.get("modifications", [])
        if m.get("date", "") >= CUTOFF_DATE
    )
    logger.info(f"Existing index: {len(existing_articles)} articles, "
                f"{existing_mod_count} modifications ({post2002_count} post-2002)")

    with LegifranceCodeClient() as client:
        # Increase delay for deep mode (many requests per article)
        client._delay = 0.1

        # Phase 1: Get CGI table of contents (fast, no leaf drilling)
        if not args.skip_toc:
            print("=== Phase 1: Fetching CGI table of contents ===")
            articles = collect_cgi_articles_fast(client)
            print(f"Found {len(articles)} articles in CGI")
        else:
            articles = []

        # Phase 2: Extract pre-2002 modifications
        print("\n=== Phase 2: Extracting pre-2002 modifications (deep mode) ===")

        if args.resume:
            articles_done, raw_mods = load_checkpoint()
        else:
            articles_done = []
            raw_mods = []

        done_set = set(articles_done)
        remaining = [a for a in articles if a["id"] not in done_set]

        if args.max_articles:
            remaining = remaining[:args.max_articles]

        total = len(remaining)
        print(f"  {total} articles to process with {args.workers} workers")

        processed = 0
        errors = 0
        import threading
        lock = threading.Lock()

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(extract_pre2002_from_article, client, art): art
                for art in remaining
            }
            for future in as_completed(futures):
                article = futures[future]
                processed += 1
                try:
                    mods = future.result()
                    with lock:
                        raw_mods.extend(mods)
                        articles_done.append(article["id"])
                except Exception as e:
                    errors += 1
                    logger.error(f"Error on {article.get('num', '?')}: {e}")
                    with lock:
                        articles_done.append(article["id"])

                if processed % 100 == 0:
                    print(f"  {processed}/{total} articles "
                          f"({len(raw_mods)} pre-2002 mods, {errors} errors)")
                if processed % 500 == 0:
                    with lock:
                        save_checkpoint(articles_done, raw_mods)

        # Final checkpoint
        if remaining:
            save_checkpoint(articles_done, raw_mods)

    print(f"\n  Done: {processed} articles processed, "
          f"{len(raw_mods)} raw pre-2002 modifications, {errors} errors")

    # Phase 3: Convert, filter and merge
    print("\n=== Phase 3: Converting, filtering and merging ===")
    by_article = convert_to_extracted_modifications(raw_mods)
    total_deduped = sum(len(v) for v in by_article.values())
    print(f"  {total_deduped} unique modifications for {len(by_article)} CGI articles (before filtering)")

    # Build whitelist: only allow articles that exist in index OR in CGI TOC
    toc_nums = set()
    if TOC_CACHE_FILE.exists():
        with open(TOC_CACHE_FILE, encoding="utf-8") as f:
            toc_articles = json.load(f)
        toc_nums = {a["num"] for a in toc_articles if a.get("num")}
    whitelist = set(existing_articles.keys()) | toc_nums
    logger.info(f"Whitelist: {len(existing_articles)} index + {len(toc_nums)} TOC = {len(whitelist)} unique")

    # Filter: only keep articles in the whitelist
    filtered = {ref: mods for ref, mods in by_article.items() if ref in whitelist}
    rejected = {ref: mods for ref, mods in by_article.items() if ref not in whitelist}
    filtered_count = sum(len(v) for v in filtered.values())
    rejected_count = sum(len(v) for v in rejected.values())
    print(f"  After whitelist filter: {filtered_count} mods for {len(filtered)} articles")
    if rejected:
        print(f"  Rejected: {rejected_count} mods for {len(rejected)} phantom articles")
        if len(rejected) <= 20:
            for ref in sorted(rejected.keys()):
                print(f"    - '{ref}' ({len(rejected[ref])} mods)")

    # Normalize existing modifications (some use loi_* instead of law_*)
    field_map = {
        "loi_numero": "law_numero",
        "loi_type": "law_type",
        "loi_annee": "law_year",
        "loi_titre": "law_title",
        "article_loi": "article_in_law",
    }
    normalized = 0
    for art_data in existing_index.get("articles", {}).values():
        for m in art_data.get("modifications", []):
            for old_key, new_key in field_map.items():
                if old_key in m:
                    m[new_key] = m.pop(old_key)
                    normalized += 1
            # Ensure all required fields exist
            m.setdefault("law_numero", "")
            m.setdefault("law_type", "")
            m.setdefault("law_year", 0)
            m.setdefault("law_title", "")
            m.setdefault("article_in_law", "")
            m.setdefault("legifrance_url", "")
    if normalized:
        print(f"  Normalized {normalized} field names in existing index")

    # Merge with existing index
    print("  Merging...")
    merged_index = build_article_index(filtered, existing_index)

    # Enrich
    print("  Enriching from reference...")
    merged_index = enrich_from_reference(merged_index, REFERENCE_PATH)
    print("  Loading law titles from DB...")
    session = get_session()
    try:
        law_titles = load_law_titles(session)
        print("  Enriching law titles...")
        merged_index = enrich_law_titles(merged_index, law_titles)
        print("  Generating keywords...")
        merged_index = generate_article_keywords(merged_index)
    finally:
        session.close()

    # Stats
    merged_articles = merged_index.get("articles", {})
    new_mod_count = sum(len(a.get("modifications", [])) for a in merged_articles.values())
    new_post2002 = sum(
        1 for a in merged_articles.values()
        for m in a.get("modifications", [])
        if m.get("date", "") >= CUTOFF_DATE
    )
    new_pre2002 = sum(
        1 for a in merged_articles.values()
        for m in a.get("modifications", [])
        if m.get("date", "") < CUTOFF_DATE
    )
    added_mods = new_mod_count - existing_mod_count
    new_art_count = len(merged_articles) - len(existing_articles)

    print(f"\nResults:")
    print(f"  Articles: {len(existing_articles)} -> {len(merged_articles)} (+{new_art_count})")
    print(f"  Modifications: {existing_mod_count} -> {new_mod_count} (+{added_mods})")
    print(f"  Pre-2002: {new_pre2002}")
    print(f"  Post-2002: {new_post2002} (was {post2002_count})")

    # Safety check: post-2002 count must not change
    if new_post2002 != post2002_count:
        logger.error(f"POST-2002 COUNT CHANGED: {post2002_count} -> {new_post2002}. Aborting.")
        return 1

    if args.dry_run:
        # Show sample
        if added_mods > 0:
            print(f"\n  Sample new modifications:")
            count = 0
            for ref, data in sorted(merged_articles.items()):
                existing_keys = set()
                if ref in existing_articles:
                    existing_keys = {
                        (m.get("law_numero", ""), m.get("article_in_law", ""))
                        for m in existing_articles[ref].get("modifications", [])
                    }
                for m in data.get("modifications", []):
                    if (m.get("law_numero", ""), m.get("article_in_law", "")) not in existing_keys:
                        print(f"    Art. {ref} <- {m.get('law_numero', '?')} ({m['date']}) [{m.get('nature', '?')}]")
                        count += 1
                        if count >= 20:
                            break
                if count >= 20:
                    break
        print("\nDry run complete. No files modified.")
        return 0

    # Write
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(merged_index, f, ensure_ascii=False, indent=2)
    logger.info(f"Index saved to {INDEX_PATH}")

    # Clean up checkpoint
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        print("Checkpoint cleaned up.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
