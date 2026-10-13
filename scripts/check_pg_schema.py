#!/usr/bin/env python3
"""Check PostgreSQL schema against SQLAlchemy models and fix mismatches.

Usage:
    DATABASE_URL=postgresql://... python scripts/check_pg_schema.py
    DATABASE_URL=postgresql://... python scripts/check_pg_schema.py --fix
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, inspect, text

# Expected schema from models.py (table -> column -> expected_type)
# Only columns where model says Text but migration might have created VARCHAR
EXPECTED_TEXT_COLUMNS = {
    "interventions": ["orateur", "fonction"],
}

# All model column definitions for full audit
MODEL_SCHEMA = {
    "lois": {
        "id": "INTEGER",
        "numero": "VARCHAR(20)",
        "type": "VARCHAR(10)",
        "annee": "INTEGER",
        "date_promulgation": "TIMESTAMP",
        "titre": "VARCHAR(500)",
        "nor": "VARCHAR(20)",
        "url_legifrance": "VARCHAR(500)",
    },
    "articles": {
        "id": "INTEGER",
        "loi_id": "INTEGER",
        "numero_article": "VARCHAR(50)",
        "contenu_texte": "TEXT",
        "position_dans_loi": "INTEGER",
    },
    "dossiers_legislatifs": {
        "id": "INTEGER",
        "loi_id": "INTEGER",
        "chambre": "VARCHAR(10)",
        "url_source": "VARCHAR(500)",
        "reference_externe": "VARCHAR(100)",
        "etapes": "JSON",
    },
    "debats": {
        "id": "INTEGER",
        "dossier_id": "INTEGER",
        "chambre": "VARCHAR(10)",
        "date": "TIMESTAMP",
        "etape": "VARCHAR(50)",
        "contenu_integral": "TEXT",
        "url_source": "VARCHAR(500)",
        "reference_externe": "VARCHAR(100)",
    },
    "interventions": {
        "id": "INTEGER",
        "debat_id": "INTEGER",
        "orateur": "TEXT",
        "fonction": "TEXT",
        "contenu": "TEXT",
        "articles_mentionnes": "JSON",
        "timestamp_debut": "TIMESTAMP",
        "ordre": "INTEGER",
    },
    "amendements": {
        "id": "INTEGER",
        "dossier_id": "INTEGER",
        "article_id": "INTEGER",
        "numero": "VARCHAR(50)",
        "auteur": "VARCHAR(500)",
        "objet": "TEXT",
        "contenu": "TEXT",
        "expose_motifs": "TEXT",
        "sort": "VARCHAR(20)",
        "date_depot": "TIMESTAMP",
        "url_source": "VARCHAR(500)",
        "reference_externe": "VARCHAR(100)",
    },
    "chunk_embeddings": {
        "id": "INTEGER",
        "chunk_type": "VARCHAR(50)",
        "content": "TEXT",
        "content_hash": "VARCHAR(64)",
        "chunk_metadata": "JSON",
        "source_id": "VARCHAR(100)",
        "position": "INTEGER",
        "embedding": "JSON",
        "embedding_model": "VARCHAR(100)",
        "embedding_dim": "INTEGER",
        "created_at": "TIMESTAMP",
        "updated_at": "TIMESTAMP",
        "intervention_id": "INTEGER",
        "amendement_id": "INTEGER",
    },
    "analysis_cache": {
        "id": "INTEGER",
        "cgi_article": "VARCHAR(50)",
        "law_numero": "VARCHAR(20)",
        "article_in_law": "VARCHAR(20)",
        "analysis_json": "JSON",
        "quality_score": "INTEGER",
        "sources_count": "INTEGER",
        "confidence_level": "VARCHAR(20)",
        "created_at": "TIMESTAMP",
        "expires_at": "TIMESTAMP",
        "embedding_model": "VARCHAR(100)",
        "llm_model": "VARCHAR(100)",
    },
    "sources_externes": {
        "id": "INTEGER",
        "type": "VARCHAR(50)",
        "titre": "VARCHAR(500)",
        "url": "VARCHAR(1000)",
        "date": "TIMESTAMP",
        "chambre": "VARCHAR(10)",
        "reference": "VARCHAR(200)",
        "auteur": "VARCHAR(500)",
        "contenu": "TEXT",
        "resume": "TEXT",
        "articles_cgi_lies": "JSON",
        "lois_liees": "JSON",
        "date_collecte": "TIMESTAMP",
        "extra_data": "JSON",
    },
}


def pg_type_to_generic(col_info: dict) -> str:
    """Convert PostgreSQL column type to generic type string for comparison."""
    pg_type = str(col_info["type"]).upper()

    if "CHARACTER VARYING" in pg_type or "VARCHAR" in pg_type:
        return pg_type.replace("CHARACTER VARYING", "VARCHAR")
    if pg_type == "TEXT":
        return "TEXT"
    if pg_type in ("INTEGER", "BIGINT", "SMALLINT"):
        return "INTEGER"
    if "TIMESTAMP" in pg_type:
        return "TIMESTAMP"
    if pg_type in ("JSON", "JSONB"):
        return "JSON"
    if pg_type in ("BOOLEAN", "BOOL"):
        return "BOOLEAN"
    return pg_type


def check_schema(engine):
    """Check all tables and columns against expected schema."""
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()

    issues = []
    fixes = []

    for table_name, expected_cols in MODEL_SCHEMA.items():
        if table_name not in existing_tables:
            issues.append(f"MISSING TABLE: {table_name}")
            continue

        db_columns = {c["name"]: c for c in inspector.get_columns(table_name)}

        for col_name, expected_type in expected_cols.items():
            if col_name not in db_columns:
                issues.append(f"MISSING COLUMN: {table_name}.{col_name} (expected {expected_type})")
                continue

            actual_type = pg_type_to_generic(db_columns[col_name])

            # Check if model expects TEXT but DB has VARCHAR
            if expected_type == "TEXT" and "VARCHAR" in actual_type:
                msg = f"TYPE MISMATCH: {table_name}.{col_name}: DB={actual_type}, Model={expected_type}"
                issues.append(msg)
                fixes.append(
                    f"ALTER TABLE {table_name} ALTER COLUMN {col_name} TYPE TEXT;"
                )
            # Check if VARCHAR lengths differ
            elif "VARCHAR" in expected_type and "VARCHAR" in actual_type:
                if expected_type != actual_type:
                    msg = f"LENGTH MISMATCH: {table_name}.{col_name}: DB={actual_type}, Model={expected_type}"
                    issues.append(msg)
                    fixes.append(
                        f"ALTER TABLE {table_name} ALTER COLUMN {col_name} TYPE {expected_type.replace('VARCHAR', 'CHARACTER VARYING')};"
                    )

        # Check for extra columns in DB not in model
        for col_name in db_columns:
            if col_name not in expected_cols:
                print(f"  INFO: Extra column in DB: {table_name}.{col_name} (not in model)")

    return issues, fixes


def main():
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("ERROR: Set DATABASE_URL environment variable.")
        print("Usage: DATABASE_URL=postgresql://... python scripts/check_pg_schema.py [--fix]")
        sys.exit(1)

    # Fix postgres:// -> postgresql:// for SQLAlchemy 2.x
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    do_fix = "--fix" in sys.argv

    engine = create_engine(db_url)

    print("Checking PostgreSQL schema against models...\n")

    # Verify connection
    with engine.connect() as conn:
        result = conn.execute(text("SELECT version()"))
        print(f"Connected: {result.scalar()}\n")

    issues, fixes = check_schema(engine)

    if not issues:
        print("All columns match the model. No fixes needed.")
        engine.dispose()
        return 0

    print(f"Found {len(issues)} issue(s):\n")
    for issue in issues:
        print(f"  {issue}")

    if fixes:
        print(f"\nSQL fixes needed ({len(fixes)}):\n")
        for fix in fixes:
            print(f"  {fix}")

        if do_fix:
            print("\nApplying fixes...")
            with engine.connect() as conn:
                for fix_sql in fixes:
                    print(f"  Executing: {fix_sql}")
                    conn.execute(text(fix_sql))
                conn.commit()
            print("\nDone. All fixes applied.")
        else:
            print("\nRun with --fix to apply these changes:")
            print(f"  DATABASE_URL=... python scripts/check_pg_schema.py --fix")

    engine.dispose()
    return 0 if not issues or do_fix else 1


if __name__ == "__main__":
    sys.exit(main())
