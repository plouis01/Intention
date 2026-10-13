"""Tests for auto-generated term expansions from article_modifications.json."""

import json
import tempfile
from pathlib import Path

import pytest

from src.indexing.search import (
    _merge_expansions,
    generate_expansions_from_index,
    reload_term_expansions,
)

# -- generate_expansions_from_index --


def test_generate_expansions_from_index_real_file():
    """Should generate expansions from the real article_modifications.json."""
    index_path = Path("data/article_modifications.json")
    if not index_path.exists():
        pytest.skip("article_modifications.json not available")

    expansions = generate_expansions_from_index(index_path)

    # Should produce non-empty result
    assert len(expansions) > 0

    # Known article should be present
    assert "150-0 B ter" in expansions
    terms = expansions["150-0 B ter"]
    assert len(terms) > 0

    # Short single words and stopwords should be filtered out
    for term in terms:
        term_lower = term.lower().strip()
        # Single words < 4 chars should not be present
        if " " not in term_lower:
            assert len(term_lower) >= 4, f"Short single word not filtered: {term!r}"


def test_generate_expansions_from_index_missing_file():
    """Should return empty dict when file does not exist."""
    result = generate_expansions_from_index(Path("/nonexistent/path.json"))
    assert result == {}


def test_generate_expansions_from_index_custom_data():
    """Should correctly extract and filter keywords from custom JSON."""
    data = {
        "articles": {
            "TestArticle": {
                "keywords": [
                    "apport-cession",          # multi-word with hyphen, keep
                    "report d'imposition",     # multi-word, keep
                    "le",                       # stopword, skip
                    "abc",                      # < 4 chars single word, skip
                    "test",                     # 4 chars, keep
                    "reinvestissement",         # long word, keep
                    "Reinvestissement",         # duplicate (case), skip
                ],
            },
            "EmptyArticle": {
                "keywords": [],
            },
        }
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(data, f)
        tmp_path = Path(f.name)

    try:
        expansions = generate_expansions_from_index(tmp_path)
        assert "TestArticle" in expansions
        assert "EmptyArticle" not in expansions

        terms = expansions["TestArticle"]
        terms_lower = [t.lower() for t in terms]
        assert "apport-cession" in terms_lower
        assert "report d'imposition" in terms_lower
        assert "test" in terms_lower
        assert "reinvestissement" in terms_lower
        # Filtered out
        assert "le" not in terms_lower
        assert "abc" not in terms_lower
        # No duplicates
        assert len(terms_lower) == len(set(terms_lower))
    finally:
        tmp_path.unlink()


def test_generate_expansions_invalid_json():
    """Should return empty dict for invalid JSON."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        f.write("{invalid json")
        tmp_path = Path(f.name)

    try:
        result = generate_expansions_from_index(tmp_path)
        assert result == {}
    finally:
        tmp_path.unlink()


# -- _merge_expansions --


def test_merge_expansions_curated_takes_priority():
    """Curated expansions should take priority over generated ones."""
    curated = {"art1": ["term_a", "term_b"]}
    generated = {"art1": ["term_b", "term_c"], "art2": ["term_d"]}

    merged = _merge_expansions(curated, generated)

    # art1 should have curated terms + new term_c from generated
    assert "term_a" in merged["art1"]
    assert "term_b" in merged["art1"]
    assert "term_c" in merged["art1"]
    # No duplicates
    assert len([t for t in merged["art1"] if t.lower() == "term_b"]) == 1

    # art2 should come from generated
    assert merged["art2"] == ["term_d"]


def test_merge_expansions_empty_curated():
    """Generated expansions should be used when curated is empty."""
    curated = {}
    generated = {"art1": ["term_a"]}
    merged = _merge_expansions(curated, generated)
    assert merged == {"art1": ["term_a"]}


def test_merge_expansions_empty_generated():
    """Curated expansions should be preserved when generated is empty."""
    curated = {"art1": ["term_a"]}
    generated = {}
    merged = _merge_expansions(curated, generated)
    assert merged == {"art1": ["term_a"]}


# -- load_term_expansions integration --


def test_load_term_expansions_merges_index():
    """load_term_expansions should include entries from both curated and index."""
    # Force reload to get fresh merge
    expansions = reload_term_expansions()

    # Should have entries from curated file
    assert "150-0 B ter" in expansions

    # If article_modifications.json exists, should have more entries than curated alone
    index_path = Path("data/article_modifications.json")
    curated_path = Path("data/legal_term_expansions.json")
    if index_path.exists() and curated_path.exists():
        with open(curated_path, encoding="utf-8") as f:
            curated_data = json.load(f)
        curated_count = len(curated_data.get("expansions", {}))
        # Merged should have at least as many as curated
        assert len(expansions) >= curated_count
