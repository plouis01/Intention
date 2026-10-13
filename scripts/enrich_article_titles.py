#!/usr/bin/env python3
"""Enrich article titles for pre-2002-only CGI articles lacking a 'titre'.

For each article in article_modifications.json without a 'titre', fetches
the article from the Légifrance API and constructs a title from the
section breadcrumb (fullSectionsTitre field).

Usage:
    python scripts/enrich_article_titles.py --dry-run --max-articles 10
    python scripts/enrich_article_titles.py --max-articles 50
    python scripts/enrich_article_titles.py
    python scripts/enrich_article_titles.py --resume
"""

import argparse
import html
import json
import logging
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx

from scripts.collect_cgi_laws import LegifranceCodeClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

INDEX_PATH = PROJECT_ROOT / "data" / "article_modifications.json"
TOC_CACHE_FILE = PROJECT_ROOT / "data" / "cgi_toc_cache.json"
CHECKPOINT_FILE = PROJECT_ROOT / "data" / "enrich_titles_checkpoint.json"
CHECKPOINT_INTERVAL = 200


def build_titre(full_sections_titre: str) -> str:
    """Build a meaningful article title from the section breadcrumb.

    Takes the last 2 meaningful segments of the breadcrumb, stripping
    structural prefixes (Livre, Titre, Chapitre, Section, Roman numerals, etc.).

    Examples:
        "... > Section I : Dispositions générales > I : Personnes imposables"
        -> "Dispositions générales - Personnes imposables"

        "... > Chapitre II : IS > Section V : Calcul de l'impôt"
        -> "Impôt sur les bénéfices des sociétés - Calcul de l'impôt"
    """
    full = html.unescape(full_sections_titre or "")
    if not full:
        return ""

    parts = [p.strip() for p in full.split(">")]
    parts = [p for p in parts if p]

    def clean(s: str) -> str:
        """Strip structural section prefixes."""
        s = re.sub(
            r"^(Livre\s+\w+\s*:\s*"
            r"|Première\s+Partie\s*:\s*|Deuxième\s+Partie\s*:\s*|Troisième\s+Partie\s*:\s*"
            r"|\w+e?\s+Partie\s*:\s*"
            r"|Titre\s+\w+\s*:\s*"
            r"|Chapitre\s+\w+\s*:\s*"
            r"|Section\s+\w+\s*:\s*"
            r"|\d+[a-z]*\s+Sous-section\s*:\s*"
            r"|Sous-section\s+\w+\s*:\s*"
            r"|[IVX]+\s*:\s*"
            r"|\d+[°\w]*\s*:\s*"
            r"|[A-Z]\s*:\s*"
            r")",
            "",
            s,
            flags=re.IGNORECASE,
        ).strip()
        return s

    cleaned = [clean(p) for p in parts]
    # Keep only segments with meaningful content (> 8 chars after cleanup)
    meaningful = [p for p in cleaned if len(p) > 8]

    if not meaningful:
        # Fallback: use original last 2 segments uncleaned
        return " - ".join(parts[-2:]) if len(parts) >= 2 else parts[-1] if parts else ""

    last_two = meaningful[-2:] if len(meaningful) >= 2 else meaningful
    return " - ".join(last_two)


def fetch_titre(client: LegifranceCodeClient, article_id: str) -> str:
    """Fetch article from API and extract a title from its section breadcrumb."""
    try:
        data = client.get_article(article_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (400, 401, 403, 404):
            return ""
        raise
    except httpx.HTTPError:
        return ""

    art = data.get("article")
    if not art or not isinstance(art, dict):
        return ""

    return build_titre(art.get("fullSectionsTitre", ""))


def save_checkpoint(done: list[str]) -> None:
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump({"done": done}, f)


def load_checkpoint() -> list[str]:
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            return json.load(f).get("done", [])
    return []


def main():
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description="Enrich article titles from Légifrance API")
    parser.add_argument("--dry-run", action="store_true", help="Show sample without writing")
    parser.add_argument("--max-articles", type=int, default=0, help="Limit articles to process (0=all)")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    args = parser.parse_args()

    # Load index
    with open(INDEX_PATH, encoding="utf-8") as f:
        data = json.load(f)
    articles = data["articles"]

    # Load TOC cache: num -> id mapping
    with open(TOC_CACHE_FILE, encoding="utf-8") as f:
        toc = json.load(f)
    toc_by_num = {a["num"]: a["id"] for a in toc if a.get("num") and a.get("id")}

    # Find articles without titre (all pre-2002-only)
    targets = [
        num for num, art in articles.items()
        if not art.get("titre", "").strip()
    ]
    logger.info(f"Articles without titre: {len(targets)}")

    # Resume support
    if args.resume:
        done_set = set(load_checkpoint())
        targets = [t for t in targets if t not in done_set]
        logger.info(f"Resuming: {len(targets)} remaining")
    else:
        done_set = set()

    # Check TOC coverage
    missing_in_toc = [t for t in targets if t not in toc_by_num]
    if missing_in_toc:
        logger.warning(f"{len(missing_in_toc)} articles not in TOC cache: {missing_in_toc[:10]}")

    processable = [t for t in targets if t in toc_by_num]
    if args.max_articles:
        processable = processable[:args.max_articles]

    print(f"Processing {len(processable)} articles")

    enriched = 0
    skipped = 0
    done_list = list(done_set)

    with LegifranceCodeClient() as client:
        client._delay = 0.1

        for i, num in enumerate(processable, 1):
            article_id = toc_by_num[num]
            titre = fetch_titre(client, article_id)

            if titre:
                if args.dry_run:
                    print(f"  [DRY] Art. {num}: {repr(titre)}")
                else:
                    articles[num]["titre"] = titre
                enriched += 1
            else:
                skipped += 1

            done_list.append(num)

            if i % 50 == 0:
                print(f"  {i}/{len(processable)} processed, {enriched} enriched, {skipped} skipped")

            if not args.dry_run and i % CHECKPOINT_INTERVAL == 0:
                save_checkpoint(done_list)
                logger.info(f"Checkpoint saved at {i} articles")

    print(f"\nResults: {enriched} titles added, {skipped} left empty (no data from API)")

    if args.dry_run:
        print("Dry run complete. No files modified.")
        return 0

    # Save
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Saved to {INDEX_PATH}")

    # Clean checkpoint
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()

    return 0


if __name__ == "__main__":
    sys.exit(main())
