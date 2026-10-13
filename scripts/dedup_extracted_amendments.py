#!/usr/bin/env python3
"""Deduplicate extracted amendments (same numero in same dossier).

The extraction script creates one record per mention of "amendement n°X"
across all debates of a dossier. This script keeps only the best record
per (dossier_id, numero) and merges metadata from duplicates.

Uses raw psycopg2 with autocommit + batch deletes to avoid timeout.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg2
from src.utils.config import get_settings

BATCH_SIZE = 5000


def get_conn():
    url = get_settings().effective_database_url
    # postgresql://localhost/legislateur_local -> host=localhost dbname=legislateur_local
    conn = psycopg2.connect(url)
    return conn


def main():
    conn = get_conn()
    conn.autocommit = True
    cur = conn.cursor()

    # Count before
    cur.execute(
        "SELECT COUNT(*) FROM amendements "
        "WHERE reference_externe LIKE 'extracted_from_debate:%%'"
    )
    before = cur.fetchone()[0]
    print(f"Extracted amendments before dedup: {before:,}")

    if before == 0:
        print("Nothing to deduplicate.")
        conn.close()
        return

    # Step 1: Merge metadata — fill sort/auteur on keepers from siblings
    print("Step 1: Merging metadata from duplicates...")

    cur.execute("""
        UPDATE amendements a
        SET sort = sub.best_sort
        FROM (
            SELECT dossier_id, numero,
                (array_agg(sort ORDER BY sort IS NULL, id))[1] AS best_sort
            FROM amendements
            WHERE reference_externe LIKE 'extracted_from_debate:%%'
            AND sort IS NOT NULL
            GROUP BY dossier_id, numero
        ) sub
        WHERE a.dossier_id = sub.dossier_id
        AND a.numero = sub.numero
        AND a.sort IS NULL
        AND a.reference_externe LIKE 'extracted_from_debate:%%'
    """)
    print(f"  Updated sort on {cur.rowcount} records")

    cur.execute("""
        UPDATE amendements a
        SET auteur = sub.best_auteur
        FROM (
            SELECT dossier_id, numero,
                (array_agg(auteur ORDER BY auteur IS NULL, id))[1] AS best_auteur
            FROM amendements
            WHERE reference_externe LIKE 'extracted_from_debate:%%'
            AND auteur IS NOT NULL
            GROUP BY dossier_id, numero
        ) sub
        WHERE a.dossier_id = sub.dossier_id
        AND a.numero = sub.numero
        AND a.auteur IS NULL
        AND a.reference_externe LIKE 'extracted_from_debate:%%'
    """)
    print(f"  Updated auteur on {cur.rowcount} records")

    # Step 2: Build temp table of IDs to delete
    print("Step 2: Identifying duplicates...")

    cur.execute("""
        CREATE TEMP TABLE _dedup_delete AS
        SELECT id FROM (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY dossier_id, numero
                ORDER BY
                    (sort IS NOT NULL)::int DESC,
                    (auteur IS NOT NULL)::int DESC,
                    LENGTH(COALESCE(objet, '')) DESC,
                    id ASC
            ) AS rn
            FROM amendements
            WHERE reference_externe LIKE 'extracted_from_debate:%%'
        ) ranked
        WHERE rn > 1
    """)

    cur.execute("SELECT COUNT(*) FROM _dedup_delete")
    to_delete_count = cur.fetchone()[0]
    print(f"  Duplicates to delete: {to_delete_count:,}")

    if to_delete_count == 0:
        print("No duplicates found.")
        cur.execute("DROP TABLE _dedup_delete")
        conn.close()
        return

    cur.execute("CREATE INDEX ON _dedup_delete(id)")

    # Step 3: Batch delete using CTID for fast sequential scans
    print("Step 3: Deleting in batches...")
    total_deleted = 0

    while total_deleted < to_delete_count:
        cur.execute(f"""
            DELETE FROM amendements
            WHERE id IN (
                SELECT id FROM _dedup_delete
                LIMIT {BATCH_SIZE}
            )
        """)
        batch_deleted = cur.rowcount

        cur.execute(f"""
            DELETE FROM _dedup_delete
            WHERE ctid IN (
                SELECT ctid FROM _dedup_delete
                LIMIT {BATCH_SIZE}
            )
        """)

        total_deleted += batch_deleted
        if batch_deleted == 0:
            break
        print(f"  Deleted {total_deleted:,}/{to_delete_count:,}...")

    cur.execute("DROP TABLE IF EXISTS _dedup_delete")
    print(f"  Total deleted: {total_deleted:,}")

    # Final stats
    cur.execute("""
        SELECT COUNT(*), COUNT(sort), COUNT(auteur)
        FROM amendements
        WHERE reference_externe LIKE 'extracted_from_debate:%%'
    """)
    total, with_sort, with_auteur = cur.fetchone()
    print(f"\nFinal: {total:,} unique amendments")
    if total > 0:
        print(f"  With sort:   {with_sort:,}/{total:,} ({with_sort/total*100:.0f}%)")
        print(f"  With auteur: {with_auteur:,}/{total:,} ({with_auteur/total*100:.0f}%)")

    conn.close()


if __name__ == "__main__":
    main()
