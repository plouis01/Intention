"""Tests for Procfile configuration - Story 3.1.

Validates Uvicorn hardening flags are present.
"""

from pathlib import Path


class TestProcfileConfig:
    """Story 3.1: Verify Uvicorn hardening in Procfile."""

    def test_procfile_exists(self):
        """Procfile must exist at project root."""
        assert Path("Procfile").exists()

    def test_procfile_has_concurrency_limit(self):
        """Procfile must include --limit-concurrency 40 (raised from 20 to match
        the dual-pool DB capacity: 16 light + 40 heavy = 56 max connections)."""
        content = Path("Procfile").read_text()
        assert "--limit-concurrency 40" in content

    def test_procfile_does_not_set_max_requests(self):
        """Procfile must NOT include --limit-max-requests.

        With a single uvicorn process and no master to respawn workers, hitting
        the request limit causes a graceful shutdown that Railway treats as a
        normal exit (no auto-restart) — leading to silent 502s. Removed.
        """
        content = Path("Procfile").read_text()
        assert "--limit-max-requests" not in content

    def test_procfile_has_host_and_port(self):
        """Procfile must bind to 0.0.0.0 and use $PORT."""
        content = Path("Procfile").read_text()
        assert "--host 0.0.0.0" in content
        assert "--port $PORT" in content
