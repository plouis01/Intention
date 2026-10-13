#!/usr/bin/env python3
"""Ensure amendements.numero is VARCHAR(50) on the configured database."""

import sys

from sqlalchemy import create_engine, text

from src.utils.config import get_settings


def get_numero_column_info(conn) -> tuple[str, int | None] | None:
    """Return (data_type, character_maximum_length) for amendements.numero."""
    row = conn.execute(
        text(
            """
            SELECT data_type, character_maximum_length
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'amendements'
              AND column_name = 'numero'
            """
        )
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1]


def main() -> int:
    settings = get_settings()
    engine = create_engine(settings.effective_database_url)

    with engine.begin() as conn:
        info_before = get_numero_column_info(conn)
        if info_before is None:
            print("ERROR: Column public.amendements.numero not found.")
            return 1

        data_type_before, max_len_before = info_before
        print(
            "Before:",
            f"data_type={data_type_before}",
            f"character_maximum_length={max_len_before}",
        )

        needs_change = (
            data_type_before in ("character varying", "character")
            and (max_len_before is None or max_len_before < 50)
        )

        if needs_change:
            conn.execute(
                text("ALTER TABLE amendements ALTER COLUMN numero TYPE VARCHAR(50)")
            )
            print("Applied: ALTER TABLE amendements ALTER COLUMN numero TYPE VARCHAR(50)")
        else:
            print("No schema change needed.")

        info_after = get_numero_column_info(conn)
        data_type_after, max_len_after = info_after
        print(
            "After:",
            f"data_type={data_type_after}",
            f"character_maximum_length={max_len_after}",
        )

        if data_type_after != "character varying" or max_len_after != 50:
            print("ERROR: Validation failed, expected character varying(50).")
            return 1

    print("Migration OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
