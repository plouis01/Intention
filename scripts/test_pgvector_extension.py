#!/usr/bin/env python3
"""
Quick test: verify pgvector extension is available in target database.

Usage:
    export TARGET_DATABASE_URL="postgresql://..."
    python scripts/test_pgvector_extension.py
"""

import os
import sys
from sqlalchemy import create_engine, text

def main():
    target_url = os.getenv("TARGET_DATABASE_URL")

    if not target_url:
        print("❌ ERROR: Set TARGET_DATABASE_URL environment variable")
        print()
        print("Example:")
        print('  export TARGET_DATABASE_URL="postgresql://postgres:xxx@host:5432/dbname"')
        sys.exit(1)

    print("🔌 Connecting to target database...")
    print(f"   URL: {target_url[:50]}...")

    try:
        engine = create_engine(target_url, pool_pre_ping=True)

        with engine.connect() as conn:
            # Try to create extension
            print("\n📦 Creating pgvector extension...")
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
            print("✅ Extension created (or already exists)")

            # Verify it exists
            print("\n🔍 Verifying pgvector extension...")
            result = conn.execute(text(
                "SELECT extname, extversion FROM pg_extension WHERE extname = 'vector'"
            ))
            row = result.fetchone()

            if row:
                print(f"✅ pgvector extension found!")
                print(f"   Name: {row[0]}")
                print(f"   Version: {row[1]}")
                print()
                print("🎉 Your database is ready for pgvector migration!")
            else:
                print("❌ pgvector extension NOT found!")
                print()
                print("⚠️  This database does NOT have pgvector support.")
                print("   Make sure you created the database with the pgvector/pgvector:pg18 template.")
                sys.exit(1)

        engine.dispose()

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
