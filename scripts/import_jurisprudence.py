#!/usr/bin/env python3
"""
Import fiscal jurisprudence (CE + CC) into sources_externes via PISTE API.

Searches Légifrance for Conseil d'État and Conseil Constitutionnel decisions
that reference CGI articles used in finance law analysis.
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from src.db.models import init_db, get_session
from src.collectors.legifrance import LegifranceClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Top CGI articles to search (most modified by finance laws)
TOP_CGI_ARTICLES = [
    "150-0 B ter", "39", "209", "219", "199 terdecies-0 A", "787 B",
    "244 quater B", "220 sexies", "238 bis", "200", "199", "156",
    "150 U", "150-0 A", "885 I ter", "150 VB", "199 undecies B",
    "44 sexies", "151 septies", "238 quindecies", "793", "795",
    "1382", "1383", "1417", "1518 A quinquies", "279-0 bis",
    "278 sexies", "244 quater C", "220 Z bis", "200 quater",
    "31", "150-0 D", "150 nonies", "93", "80 quinquies",
    "223 A", "223 B", "210 F", "39 decies", "44 quindecies",
]


def search_jurisprudence(client: LegifranceClient, fond: str, article_ref: str, max_results: int = 20) -> list[dict]:
    """Search for jurisprudence decisions mentioning a CGI article."""
    try:
        # Use the PISTE search endpoint
        search_body = {
            "fond": fond,
            "recherche": {
                "champs": [
                    {
                        "typeChamp": "ALL",
                        "criteres": [
                            {
                                "typeRecherche": "EXACTE",
                                "valeur": f"article {article_ref} du code général des impôts",
                                "operateur": "ET",
                            }
                        ],
                    }
                ],
                "filtres": [],
                "pageNumber": 1,
                "pageSize": max_results,
                "sort": "PERTINENCE",
                "typePagination": "DEFAUT",
            },
        }

        resp = client._post("/search", search_body)
        if not resp:
            return []

        results = resp.get("results", [])
        decisions = []

        for r in results:
            titles = r.get("titles", {})
            titre = titles.get("titreLong", titles.get("title", ""))

            decision = {
                "titre": titre[:500] if titre else "",
                "reference": r.get("id", ""),
                "date": r.get("date", ""),
                "url": f"https://www.legifrance.gouv.fr/{fond.lower()}/id/{r.get('id', '')}",
                "fond": fond,
                "article_cgi": article_ref,
            }
            decisions.append(decision)

        return decisions

    except Exception as e:
        logger.debug(f"Search error for {fond}/{article_ref}: {e}")
        return []


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fond", choices=["JADE", "CONSTIT", "ALL"], default="ALL")
    args = parser.parse_args()

    fonds = ["JADE", "CONSTIT"] if args.fond == "ALL" else [args.fond]

    try:
        client = LegifranceClient()
    except Exception as e:
        logger.error(f"Cannot initialize Légifrance client: {e}")
        return

    init_db()
    session = get_session()

    # Get existing jurisprudence URLs
    existing_urls = set()
    result = session.execute(text(
        "SELECT url FROM sources_externes WHERE type = 'jurisprudence'"
    ))
    for (url,) in result:
        existing_urls.add(url)

    total_found = 0
    total_new = 0

    for fond in fonds:
        chambre = "CE" if fond == "JADE" else "CC"
        logger.info(f"\n=== Searching {fond} ({chambre}) ===")

        for article_ref in TOP_CGI_ARTICLES:
            decisions = search_jurisprudence(client, fond, article_ref)
            new_decisions = [d for d in decisions if d["url"] not in existing_urls]

            if decisions:
                logger.info(f"  Art. {article_ref}: {len(decisions)} found, {len(new_decisions)} new")

            for d in new_decisions:
                total_found += 1
                if not args.dry_run:
                    date_val = None
                    if d.get("date"):
                        try:
                            date_val = datetime.fromisoformat(d["date"].replace("Z", "+00:00"))
                        except (ValueError, TypeError):
                            pass

                    session.execute(text("""
                        INSERT INTO sources_externes (type, titre, url, date, chambre, reference, articles_cgi_lies, date_collecte)
                        VALUES ('jurisprudence', :titre, :url, :date, :chambre, :ref, :articles, now())
                    """), {
                        "titre": d["titre"],
                        "url": d["url"],
                        "date": date_val,
                        "chambre": chambre,
                        "ref": d["reference"],
                        "articles": json.dumps([d["article_cgi"]]),
                    })
                    existing_urls.add(d["url"])
                    total_new += 1

            time.sleep(1)  # Rate limiting

        if not args.dry_run:
            session.commit()

    logger.info(f"\n{'Would import' if args.dry_run else 'Imported'} {total_found} jurisprudence decisions")
    session.close()


if __name__ == "__main__":
    main()
