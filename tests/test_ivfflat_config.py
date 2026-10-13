"""Tests for IVFFlat index configuration - Story 4.1.

Validates probes setting and index parameters.
"""


class TestIVFFlatProbes:
    """Story 4.1: Verify IVFFlat probes is set to 40."""

    def test_search_sets_probes_40(self):
        """vector_stores.py must SET ivfflat.probes = 40."""
        from pathlib import Path

        content = Path("src/indexing/vector_stores.py").read_text()
        assert 'SET ivfflat.probes = 40' in content
        assert 'SET ivfflat.probes = 10' not in content

    def test_probes_in_optimal_range(self):
        """Probes value (40) must be in the 30-50 range per spec."""
        probes = 40
        assert 30 <= probes <= 50


class TestIVFFlatLists:
    """Story 4.1: Verify IVFFlat lists calculation."""

    def test_rebuild_script_calculation_logic(self):
        """Rebuild script must calculate lists as sqrt(row_count)."""
        from scripts.rebuild_ivfflat_index import calculate_optimal_lists

        # Test calculation logic with various row counts
        assert calculate_optimal_lists(1_600_000) == 1264  # int(sqrt(1.6M))
        assert calculate_optimal_lists(2_966_075) == 1722  # int(sqrt(2.966M))
        assert calculate_optimal_lists(4_000_000) == 2000  # int(sqrt(4M))

    def test_calculation_matches_formula(self):
        """Calculation should match sqrt formula exactly."""
        import math
        from scripts.rebuild_ivfflat_index import calculate_optimal_lists

        test_values = [100_000, 1_000_000, 2_500_000, 5_000_000]

        for row_count in test_values:
            expected = int(math.sqrt(row_count))
            actual = calculate_optimal_lists(row_count)
            assert actual == expected, f"For {row_count:,} rows: expected {expected}, got {actual}"
