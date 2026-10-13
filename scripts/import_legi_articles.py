#!/usr/bin/env python3
"""
Import articles from legi_articles fallback for pre-2002 laws.

Targets laws that have legi_articles in their Légifrance JSON
but no articles imported in the database yet.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.import_data import import_articles_from_legifrance
from src.db.models import Article, Loi, get_session

PROJECT_ROOT = Path(__file__).parent.parent
LEGIFRANCE_DIR = PROJECT_ROOT / "data" / "raw" / "legifrance"


def main():
    session = get_session()

    # Find laws without articles
    laws_with_articles = {
        row[0] for row in session.query(Article.loi_id).distinct().all()
    }
    all_laws = session.query(Loi).all()
    laws_missing = [l for l in all_laws if l.id not in laws_with_articles]

    print(f"Total laws: {len(all_laws)}")
    print(f"Laws with articles: {len(laws_with_articles)}")
    print(f"Laws missing articles: {len(laws_missing)}")
    print()

    imported_total = 0
    for loi in laws_missing:
        articles = import_articles_from_legifrance(session, loi, LEGIFRANCE_DIR)
        if articles:
            print(f"  + {loi.numero} ({loi.date_promulgation}): {len(articles)} articles")
            imported_total += len(articles)

    session.commit()
    session.close()

    print(f"\nImported {imported_total} articles for {len(laws_missing)} laws")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    main()
