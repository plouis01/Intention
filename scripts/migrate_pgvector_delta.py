#!/usr/bin/env python3
"""
Delta migration: sync new chunks from source DB to pgvector DB.
Only migrates chunks that don't exist in target (based on ID or content_hash).

Usage:
    export SOURCE_DATABASE_URL="postgresql://..."
    export TARGET_DATABASE_URL="postgresql://..."
    python scripts/migrate_pgvector_delta.py [--dry-run]
"""

import os
import sys
import json
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime
from urllib.parse import urlparse

BATCH_SIZE = 10_000
DRY_RUN = "--dry-run" in sys.argv

def parse_db_url(url):
    parsed = urlparse(url)
    return {
        'host': parsed.hostname,
        'port': parsed.port or 5432,
        'database': parsed.path[1:],
        'user': parsed.username,
        'password': parsed.password
    }

def main():
    source_url = os.getenv("SOURCE_DATABASE_URL")
    target_url = os.getenv("TARGET_DATABASE_URL")

    if not source_url or not target_url:
        print("❌ ERROR: Set SOURCE_DATABASE_URL and TARGET_DATABASE_URL")
        sys.exit(1)

    mode = "DRY RUN" if DRY_RUN else "LIVE"
    print("=" * 60)
    print(f"DELTA MIGRATION: {mode}")
    print("=" * 60)

    source_params = parse_db_url(source_url)
    target_params = parse_db_url(target_url)

    print("\n🔌 Connecting to databases...")
    source_conn = psycopg2.connect(**source_params)
    target_conn = psycopg2.connect(**target_params)

    source_cur = source_conn.cursor()
    target_cur = target_conn.cursor()

    try:
        # Step 1: Count chunks in both DBs
        print("\n📊 Comparing databases...")

        source_cur.execute("""
            SELECT COUNT(*) FROM chunk_embeddings
            WHERE embedding_model = 'paraphrase-multilingual-MiniLM-L12-v2'
        """)
        source_count = source_cur.fetchone()[0]

        target_cur.execute("SELECT COUNT(*) FROM chunk_embeddings")
        target_count = target_cur.fetchone()[0]

        print(f"   Source DB: {source_count:,} chunks")
        print(f"   Target DB: {target_count:,} chunks")
        print(f"   Delta: {source_count - target_count:,} chunks")

        if source_count <= target_count:
            print("\n✅ Target DB is up-to-date! Nothing to migrate.")
            return

        # Step 2: Get max ID from target
        target_cur.execute("SELECT COALESCE(MAX(id), 0) FROM chunk_embeddings")
        max_target_id = target_cur.fetchone()[0]
        print(f"\n🔍 Max ID in target: {max_target_id:,}")

        # Step 3: Find new chunks in source (ID > max_target_id)
        print(f"\n🔎 Finding new chunks (ID > {max_target_id:,})...")

        source_cur.execute("""
            SELECT COUNT(*) FROM chunk_embeddings
            WHERE embedding_model = 'paraphrase-multilingual-MiniLM-L12-v2'
            AND id > %s
        """, (max_target_id,))
        new_chunks = source_cur.fetchone()[0]

        print(f"   New chunks to migrate: {new_chunks:,}")

        if new_chunks == 0:
            print("\n✅ No new chunks to migrate!")
            return

        if DRY_RUN:
            print("\n🔍 DRY RUN MODE - No actual migration")
            print(f"   Would migrate {new_chunks:,} chunks")
            return

        # Step 4: Migrate new chunks
        print(f"\n🚀 Migrating {new_chunks:,} new chunks...")

        migrated_count = 0
        offset = 0
        start_time = datetime.now()

        while True:
            batch_start = datetime.now()

            # Fetch batch from source (only new chunks)
            source_cur.execute(f"""
                SELECT
                    chunk_type, content, content_hash, chunk_metadata,
                    source_id, position, embedding,
                    embedding_model, embedding_dim,
                    created_at, updated_at, intervention_id, amendement_id
                FROM chunk_embeddings
                WHERE embedding_model = 'paraphrase-multilingual-MiniLM-L12-v2'
                AND id > %s
                ORDER BY id
                LIMIT %s OFFSET %s
            """, (max_target_id, BATCH_SIZE, offset))

            rows = source_cur.fetchall()
            if not rows:
                break

            # Prepare batch values
            values = []
            for row in rows:
                (chunk_type, content, content_hash, chunk_metadata,
                 source_id, position, embedding,
                 embedding_model, embedding_dim,
                 created_at, updated_at, intervention_id, amendement_id) = row

                embedding_list = json.loads(embedding) if isinstance(embedding, str) else embedding
                vector_str = f"[{','.join(map(str, embedding_list))}]"
                metadata_str = json.dumps(chunk_metadata) if chunk_metadata else None

                values.append((
                    chunk_type, content, content_hash, metadata_str,
                    source_id, position, json.dumps(embedding), vector_str,
                    embedding_model, embedding_dim,
                    created_at, updated_at, intervention_id, amendement_id
                ))

            # Bulk insert
            execute_values(
                target_cur,
                """
                INSERT INTO chunk_embeddings (
                    chunk_type, content, content_hash, chunk_metadata,
                    source_id, position, embedding, embedding_vector,
                    embedding_model, embedding_dim,
                    created_at, updated_at, intervention_id, amendement_id
                ) VALUES %s
                """,
                values,
                template="""(
                    %s, %s, %s, %s::jsonb,
                    %s, %s, %s::jsonb, %s::vector,
                    %s, %s, %s, %s, %s, %s
                )""",
                page_size=BATCH_SIZE
            )
            target_conn.commit()

            migrated_count += len(rows)
            offset += BATCH_SIZE

            batch_duration = (datetime.now() - batch_start).total_seconds()
            rate = len(rows) / batch_duration if batch_duration > 0 else 0

            progress = (migrated_count / new_chunks) * 100
            print(f"   [{progress:5.1f}%] {migrated_count:,}/{new_chunks:,} chunks | "
                  f"{rate:,.0f} chunks/sec | {batch_duration:.1f}s")

        total_duration = (datetime.now() - start_time).total_seconds()

        # Verify
        target_cur.execute("SELECT COUNT(*) FROM chunk_embeddings")
        final_count = target_cur.fetchone()[0]

        print("\n" + "=" * 60)
        print("✅ DELTA MIGRATION COMPLETED")
        print("=" * 60)
        print(f"📊 Before: {target_count:,} chunks")
        print(f"📊 After: {final_count:,} chunks")
        print(f"📊 Migrated: {migrated_count:,} chunks")
        print(f"⏱️  Time: {total_duration:.1f}s ({migrated_count / total_duration:,.0f} chunks/sec)")

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        target_conn.rollback()
        sys.exit(1)

    finally:
        source_cur.close()
        target_cur.close()
        source_conn.close()
        target_conn.close()

if __name__ == "__main__":
    main()
