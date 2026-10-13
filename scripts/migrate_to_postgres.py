"""Migrate data from local SQLite to Railway PostgreSQL."""

import os
import sqlite3
import sys
import time

import psycopg2
import psycopg2.extras

SQLITE_PATH = "data/legislateur.db"
PG_URL = os.environ.get("DATABASE_URL", "")
if not PG_URL:
    print("ERROR: Set DATABASE_URL environment variable to the PostgreSQL connection string.")
    sys.exit(1)

# Tables in insertion order (respects foreign key dependencies)
TABLES_IN_ORDER = [
    "lois",
    "articles",
    "dossiers_legislatifs",
    "debats",
    "interventions",
    "amendements",
    "sources_externes",
    "chunk_embeddings",
    "analysis_cache",
]

BATCH_SIZE = 5000


def get_sqlite_schema(cursor, table):
    """Get column names for a table."""
    cursor.execute(f'PRAGMA table_info("{table}")')
    return [row[1] for row in cursor.fetchall()]


def get_sqlite_count(cursor, table):
    cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
    return cursor.fetchone()[0]


def create_pg_schema(pg_conn):
    """Create all tables in PostgreSQL using SQLAlchemy models."""
    # Use SQLAlchemy to create schema on the PG database
    from sqlalchemy import create_engine
    sys.path.insert(0, ".")
    from src.db.models import Base

    engine = create_engine(PG_URL)
    Base.metadata.create_all(engine)
    engine.dispose()
    print("PostgreSQL schema created.")


def adapt_row(row, columns):
    """Convert SQLite row values for PostgreSQL compatibility."""
    adapted = list(row)
    for i, val in enumerate(adapted):
        # SQLite stores booleans as 0/1
        if isinstance(val, bytes):
            adapted[i] = val.decode("utf-8", errors="replace")
    return tuple(adapted)


def migrate_table(sqlite_cur, pg_conn, table):
    columns = get_sqlite_schema(sqlite_cur, table)
    total = get_sqlite_count(sqlite_cur, table)

    if total == 0:
        print(f"  {table}: 0 rows (skipping)")
        return

    col_list = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    insert_sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'

    pg_cur = pg_conn.cursor()
    sqlite_cur.execute(f'SELECT * FROM "{table}"')

    migrated = 0
    start = time.time()

    while True:
        rows = sqlite_cur.fetchmany(BATCH_SIZE)
        if not rows:
            break
        adapted = [adapt_row(r, columns) for r in rows]
        psycopg2.extras.execute_batch(pg_cur, insert_sql, adapted, page_size=1000)
        pg_conn.commit()
        migrated += len(rows)
        elapsed = time.time() - start
        rate = migrated / elapsed if elapsed > 0 else 0
        print(f"  {table}: {migrated}/{total} ({migrated*100//total}%) - {rate:.0f} rows/s", end="\r")

    # Reset PG sequences to max id
    if "id" in columns:
        pg_cur.execute(f'SELECT MAX(id) FROM "{table}"')
        max_id = pg_cur.fetchone()[0]
        if max_id:
            pg_cur.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), {max_id})")
            pg_conn.commit()

    elapsed = time.time() - start
    print(f"  {table}: {migrated}/{total} (100%) - done in {elapsed:.1f}s          ")


def verify(sqlite_cur, pg_conn):
    """Compare row counts between SQLite and PostgreSQL."""
    print("\nVerification:")
    pg_cur = pg_conn.cursor()
    all_ok = True
    for table in TABLES_IN_ORDER:
        sqlite_count = get_sqlite_count(sqlite_cur, table)
        pg_cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        pg_count = pg_cur.fetchone()[0]
        status = "OK" if sqlite_count == pg_count else "MISMATCH"
        if status == "MISMATCH":
            all_ok = False
        print(f"  {table}: SQLite={sqlite_count} PG={pg_count} [{status}]")
    return all_ok


def main():
    print(f"Source: {SQLITE_PATH}")
    print(f"Target: PostgreSQL on Railway\n")

    # Connect to both databases
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_cur = sqlite_conn.cursor()

    pg_conn = psycopg2.connect(PG_URL)

    # Step 1: Create schema
    print("Step 1: Creating PostgreSQL schema...")
    create_pg_schema(pg_conn)

    # Step 2: Migrate each table
    print("\nStep 2: Migrating data...")
    total_start = time.time()
    for table in TABLES_IN_ORDER:
        migrate_table(sqlite_cur, pg_conn, table)

    total_elapsed = time.time() - total_start
    print(f"\nMigration completed in {total_elapsed:.1f}s")

    # Step 3: Verify
    ok = verify(sqlite_cur, pg_conn)

    sqlite_conn.close()
    pg_conn.close()

    if ok:
        print("\nAll counts match. Migration successful!")
    else:
        print("\nSome counts don't match - check above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
