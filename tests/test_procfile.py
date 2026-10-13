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
        """Procfile must include --limit-concurrency 20."""
        content = Path("Procfile").read_text()
        assert "--limit-concurrency 20" in content

    def test_procfile_has_max_requests(self):
        """Procfile must include --limit-max-requests 5000."""
        content = Path("Procfile").read_text()
        assert "--limit-max-requests 5000" in content

    def test_procfile_has_host_and_port(self):
        """Procfile must bind to 0.0.0.0 and use $PORT."""
        content = Path("Procfile").read_text()
        assert "--host 0.0.0.0" in content
        assert "--port $PORT" in content
