#!/usr/bin/env python3
"""Backfill articles_mentionnes for existing interventions that have NULL.

Uses a single UPDATE ... FROM (VALUES ...) per batch to minimize round-trips
to the remote database.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.disable(logging.INFO)

from sqlalchemy import text

from src.db.models import get_engine
from src.parsers.debates import extract_article_mentions


BATCH_SIZE = 1000


def backfill(dry_run: bool = False, limit: int = 0):
    """Backfill articles_mentionnes on interventions where it is NULL."""
    engine = get_engine()

    with engine.connect() as conn:
        total_null = conn.execute(
            text("SELECT count(*) FROM interventions WHERE articles_mentionnes IS NULL")
        ).scalar()

        print(f"NULL articles_mentionnes: {total_null}")

        if total_null == 0:
            print("Nothing to backfill.")
            return

        processed = 0
        updated_with_articles = 0
        start_time = time.time()

        while True:
            # Fetch batch: only id + contenu
            rows = conn.execute(
                text(
                    "SELECT id, contenu FROM interventions "
                    "WHERE articles_mentionnes IS NULL "
                    "ORDER BY id LIMIT :batch_size"
                ),
                {"batch_size": BATCH_SIZE},
            ).fetchall()

            if not rows:
                break

            # Compute mentions in Python
            values_parts = []
            params = {}
            for idx, (row_id, contenu) in enumerate(rows):
                mentions = extract_article_mentions(contenu) if contenu else []
                mentions_json = json.dumps(mentions)
                params[f"id_{idx}"] = row_id
                params[f"m_{idx}"] = mentions_json
                values_parts.append(f"(:id_{idx}, CAST(:m_{idx} AS json))")
                if mentions:
                    updated_with_articles += 1
                processed += 1
                if limit and processed >= limit:
                    break

            # Single UPDATE with FROM VALUES - one round-trip
            if not dry_run and values_parts:
                values_sql = ", ".join(values_parts)
                conn.execute(
                    text(
                        f"UPDATE interventions AS i "
                        f"SET articles_mentionnes = v.mentions "
                        f"FROM (VALUES {values_sql}) AS v(vid, mentions) "
                        f"WHERE i.id = v.vid"
                    ),
                    params,
                )
                conn.commit()

            elapsed = time.time() - start_time
            rate = processed / elapsed if elapsed > 0 else 0
            pct = 100 * processed / total_null
            print(
                f"  {processed}/{total_null} ({pct:.1f}%) "
                f"- {updated_with_articles} with articles "
                f"[{rate:.0f}/s]"
            )

            if limit and processed >= limit:
                break

        elapsed = time.time() - start_time
        print(f"\n--- Done ---")
        print(f"Processed: {processed}")
        print(f"With article mentions: {updated_with_articles} ({100*updated_with_articles/max(processed,1):.1f}%)")
        print(f"Time: {elapsed:.1f}s ({processed/elapsed:.0f}/s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill articles_mentionnes")
    parser.add_argument("--dry-run", action="store_true", help="Don't commit changes")
    parser.add_argument("--limit", type=int, default=0, help="Max interventions to process")
    args = parser.parse_args()

    print("Backfilling articles_mentionnes...\n")
    backfill(dry_run=args.dry_run, limit=args.limit)
