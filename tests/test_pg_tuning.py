"""Tests for PostgreSQL memory tuning - Story 3.2.

Validates tuning parameters match the specification.
"""


class TestPGTuningConfig:
    """Story 3.2: Verify PostgreSQL tuning parameters."""

    def test_tuning_params_match_spec(self):
        """Tuning parameters must match the product brief specification."""
        from scripts.tune_postgresql_memory import PG_TUNING

        assert PG_TUNING["shared_buffers"] == "128MB"
        assert PG_TUNING["work_mem"] == "4MB"
        assert PG_TUNING["effective_cache_size"] == "512MB"
        assert PG_TUNING["maintenance_work_mem"] == "64MB"
        assert PG_TUNING["max_connections"] == "30"

    def test_pool_size_fits_max_connections(self):
        """Connection pool (5 + 10 overflow = 15) must fit within max_connections (30)."""
        from scripts.tune_postgresql_memory import PG_TUNING

        max_conn = int(PG_TUNING["max_connections"])
        pool_total = 5 + 10  # pool_size + max_overflow from Story 1.1
        assert pool_total < max_conn, (
            f"Pool total ({pool_total}) must be less than max_connections ({max_conn})"
        )
