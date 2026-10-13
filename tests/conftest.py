"""Global pytest configuration.

Runs BEFORE any test module imports `src.*`. Forces a disposable SQLite
database so tests never touch the developer's real Postgres DB (that's how
the `chunk_embeddings` table got wiped in an earlier iteration - fixtures
called `reset_db()` against whatever `DATABASE_URL` the .env pointed to).
"""

from __future__ import annotations

import os
import pathlib
import tempfile


def _configure_test_database() -> None:
    # Isolated per-worker file so `pytest-xdist` or repeated runs don't clash.
    tmp_dir = pathlib.Path(tempfile.gettempdir())
    db_path = tmp_dir / f"legislateur_pytest_{os.getpid()}.db"
    if db_path.exists():
        db_path.unlink()
    # Force DATABASE_URL no matter what .env says. The substring "test"
    # doubles as the safety-rail accepted by reset_db().
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    # Satisfy other modules that require these at import time.
    os.environ.setdefault("JWT_SECRET_KEY", "test-secret-do-not-use-in-prod")
    os.environ.setdefault("ENVIRONMENT", "development")


_configure_test_database()
