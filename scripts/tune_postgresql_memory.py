"""PostgreSQL memory tuning for Railway - Story 3.2.

Applies ALTER SYSTEM commands to tune PostgreSQL memory parameters
for Railway's constrained environment (1-2 GB RAM target).

Usage:
    python scripts/tune_postgresql_memory.py          # Apply tuning
    python scripts/tune_postgresql_memory.py --check   # Check current values
    python scripts/tune_postgresql_memory.py --reset   # Reset to defaults

Note: PostgreSQL must be restarted after ALTER SYSTEM for changes to take effect.
On Railway, this happens automatically on the next deploy or manual restart.
"""

import argparse
import sys

from sqlalchemy import create_engine, text

from src.utils.config import get_settings

# Target configuration for Railway (1-2 GB RAM budget)
PG_TUNING = {
    "shared_buffers": "128MB",
    "work_mem": "4MB",
    "effective_cache_size": "512MB",
    "maintenance_work_mem": "64MB",
    "max_connections": "30",
}


def get_pg_engine():
    """Get PostgreSQL engine from settings."""
    settings = get_settings()
    url = settings.effective_database_url
    if not url.startswith("postgresql"):
        print(f"ERROR: Not a PostgreSQL URL: {url[:30]}...")
        sys.exit(1)
    return create_engine(url)


def apply_tuning():
    """Apply ALTER SYSTEM commands for memory tuning."""
    engine = get_pg_engine()
    with engine.connect() as conn:
        for param, value in PG_TUNING.items():
            stmt = text(f"ALTER SYSTEM SET {param} = :value")
            conn.execute(stmt, {"value": value})
            print(f"  SET {param} = {value}")
        conn.commit()
    print("\nTuning applied. Restart PostgreSQL for changes to take effect.")
    print("On Railway: redeploy or restart the PostgreSQL service.")


def check_current():
    """Show current PostgreSQL parameter values."""
    engine = get_pg_engine()
    with engine.connect() as conn:
        print("Current PostgreSQL settings:")
        for param in PG_TUNING:
            result = conn.execute(text(f"SHOW {param}"))
            current = result.scalar()
            target = PG_TUNING[param]
            status = "OK" if current == target else f"MISMATCH (target: {target})"
            print(f"  {param} = {current}  [{status}]")


def reset_defaults():
    """Reset all tuned parameters to PostgreSQL defaults."""
    engine = get_pg_engine()
    with engine.connect() as conn:
        for param in PG_TUNING:
            conn.execute(text(f"ALTER SYSTEM RESET {param}"))
            print(f"  RESET {param}")
        conn.commit()
    print("\nParameters reset to defaults. Restart PostgreSQL for changes to take effect.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PostgreSQL memory tuning for Railway")
    parser.add_argument("--check", action="store_true", help="Check current values")
    parser.add_argument("--reset", action="store_true", help="Reset to defaults")
    args = parser.parse_args()

    if args.check:
        check_current()
    elif args.reset:
        reset_defaults()
    else:
        apply_tuning()
