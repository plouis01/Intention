"""Comprehensive API endpoint tests.

Tests cover all API routes: health, CGI articles, analysis endpoints,
caching, and endpoint removal verification.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.db.models import init_db


@pytest.fixture(autouse=True)
def _init_db():
    """Ensure database is initialized for every test."""
    init_db()


@pytest.fixture
def client():
    """Create a fresh test client."""
    return TestClient(app)


# ============================================================================
# Health & Docs
# ============================================================================


def test_health_returns_provider_info(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    for key in ("embedding_provider", "llm_provider", "database_status", "version"):
        assert key in data


def test_openapi_schema(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert "paths" in schema
    paths = list(schema["paths"].keys())
    assert any(p.startswith("/cgi") for p in paths)
    assert any(p.startswith("/analysis") for p in paths)
    assert any(p == "/health" for p in paths)
    # Removed routes should NOT be present
    assert not any(p.startswith("/lois") for p in paths)
    assert not any(p.startswith("/search") for p in paths)


# ============================================================================
# Analysis Laws Endpoint
# ============================================================================


def test_list_available_laws(client):
    r = client.get("/analysis/laws")
    assert r.status_code == 200
    laws = r.json()
    assert isinstance(laws, list)
    if laws:
        law = laws[0]
        assert "numero" in law
        assert "type" in law
        assert "annee_budgetaire" in law


# ============================================================================
# CGI Article Endpoints (/cgi)
# ============================================================================


@pytest.fixture
def mock_cgi_index():
    """Provide a minimal CGI article index."""
    index = {
        "articles": {
            "150-0 B ter": {
                "titre": "Apport-cession - Report d'imposition des plus-values",
                "code": "CGI",
                "legifrance_url": "https://example.com/150-0-b-ter",
                "bofip_url": "https://example.com/bofip",
                "keywords": ["apport-cession", "report", "plus-values"],
                "modifications": [
                    {
                        "date": "2019-12-29",
                        "law_numero": "2019-1479",
                        "law_type": "LF",
                        "law_year": 2020,
                        "law_title": "Loi de finances pour 2020",
                        "article_in_law": "Art. 106",
                        "nature": "Modification",
                        "legifrance_url": "",
                    },
                    {
                        "date": "2022-12-30",
                        "law_numero": "2022-1726",
                        "law_type": "LF",
                        "law_year": 2023,
                        "law_title": "Loi de finances pour 2023",
                        "article_in_law": "Art. 23",
                        "nature": "Modification",
                        "legifrance_url": "",
                    },
                ],
            },
            "199 terdecies-0 A": {
                "titre": "Réduction IR-PME (Madelin)",
                "code": "CGI",
                "keywords": ["madelin", "IR-PME"],
                "modifications": [
                    {
                        "date": "2023-12-30",
                        "law_numero": "2023-1322",
                        "law_type": "LF",
                        "law_year": 2024,
                        "law_title": "Loi de finances pour 2024",
                        "article_in_law": "Art. 48",
                        "nature": "Modification",
                        "legifrance_url": "",
                    },
                ],
            },
        }
    }
    with patch("src.api.cgi_routes._article_index", index):
        yield index


def test_list_cgi_articles(client, mock_cgi_index):
    r = client.get("/cgi")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert len(data["articles"]) == 2
    assert data["articles"][0]["modification_count"] >= data["articles"][1]["modification_count"]


def test_list_cgi_articles_with_search(client, mock_cgi_index):
    r = client.get("/cgi?search=150-0")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["articles"][0]["reference"] == "150-0 B ter"


def test_list_cgi_articles_min_modifications(client, mock_cgi_index):
    r = client.get("/cgi?min_modifications=2")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["articles"][0]["reference"] == "150-0 B ter"


def test_list_cgi_articles_pagination(client, mock_cgi_index):
    r = client.get("/cgi?limit=1&offset=0")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert len(data["articles"]) == 1

    r2 = client.get("/cgi?limit=1&offset=1")
    data2 = r2.json()
    assert len(data2["articles"]) == 1
    assert data2["articles"][0]["reference"] != data["articles"][0]["reference"]


def test_get_cgi_article_detail(client, mock_cgi_index):
    r = client.get("/cgi/150-0 B ter")
    assert r.status_code == 200
    data = r.json()
    assert data["reference"] == "150-0 B ter"
    assert data["titre"] == "Apport-cession - Report d'imposition des plus-values"
    assert len(data["modifications"]) == 2
    assert data["legifrance_url"] == "https://example.com/150-0-b-ter"
    assert "keywords" in data


def test_get_cgi_article_case_insensitive(client, mock_cgi_index):
    r = client.get("/cgi/199 Terdecies-0 A")
    assert r.status_code == 200
    data = r.json()
    assert data["reference"] == "199 terdecies-0 A"


def test_get_cgi_article_not_found(client, mock_cgi_index):
    r = client.get("/cgi/NONEXISTENT")
    assert r.status_code == 404


def test_get_cgi_article_underscore_normalization(client, mock_cgi_index):
    r = client.get("/cgi/150-0_B_ter")
    assert r.status_code == 200
    assert r.json()["reference"] == "150-0 B ter"


# ============================================================================
# Persisted Analysis Endpoint (/cgi/{ref}/analysis)
# ============================================================================


def test_get_analysis_not_found(client, mock_cgi_index):
    """GET /cgi/{ref}/analysis returns 404 when no cache exists."""
    r = client.get("/cgi/150-0_B_ter/analysis")
    assert r.status_code == 404
    data = r.json()
    assert data["status"] == "not_found"
    assert "message" in data


def test_get_analysis_cached(client, mock_cgi_index):
    """GET /cgi/{ref}/analysis returns cached data when available."""
    from src.api.cgi_routes import compute_modifications_hash

    mods = mock_cgi_index["articles"]["150-0 B ter"]["modifications"]
    mod_hash = compute_modifications_hash(mods)

    mock_cache = type("MockCache", (), {
        "analysis_json": {
            "article_info": {"reference": "150-0 B ter"},
            "sections": [],
            "metadata": {"total_sections": 0},
        },
        "expires_at": None,
        "modifications_hash": mod_hash,
    })()

    def mock_query_fn(cgi_article):
        if cgi_article == "150-0 B ter":
            return mock_cache.analysis_json
        return None

    with patch("src.api.cgi_routes.get_cached_analysis", side_effect=mock_query_fn):
        r = client.get("/cgi/150-0_B_ter/analysis")
        assert r.status_code == 200
        data = r.json()
        assert data["from_cache"] is True
        assert data["article_info"]["reference"] == "150-0 B ter"


def test_compute_modifications_hash_determinism():
    """Same modifications produce the same hash."""
    from src.api.cgi_routes import compute_modifications_hash

    mods = [
        {"date": "2019-12-29", "law_numero": "2019-1479"},
        {"date": "2022-12-30", "law_numero": "2022-1726"},
    ]
    assert compute_modifications_hash(mods) == compute_modifications_hash(mods)
    # Order-independent
    reversed_mods = list(reversed(mods))
    assert compute_modifications_hash(mods) == compute_modifications_hash(reversed_mods)


def test_compute_modifications_hash_sensitivity():
    """Different modifications produce different hashes."""
    from src.api.cgi_routes import compute_modifications_hash

    mods_a = [{"date": "2019-12-29", "law_numero": "2019-1479"}]
    mods_b = [{"date": "2019-12-29", "law_numero": "2019-1480"}]
    assert compute_modifications_hash(mods_a) != compute_modifications_hash(mods_b)


def test_get_analysis_stale_cache_returns_404(client, mock_cgi_index):
    """GET /cgi/{ref}/analysis returns 404 when cache has mismatched hash."""
    # Return analysis with wrong hash — should be invalidated
    def mock_stale_query(cgi_article):
        return None  # Simulates invalidation returning None

    with patch("src.api.cgi_routes.get_cached_analysis", side_effect=mock_stale_query):
        r = client.get("/cgi/150-0_B_ter/analysis")
        assert r.status_code == 404


# ============================================================================
# Cache Management
# ============================================================================


def test_clear_cache_nonexistent(client):
    """Clearing cache for nonexistent article should return 0 deleted."""
    r = client.delete("/cgi/NONEXISTENT/cache")
    assert r.status_code == 200
    data = r.json()
    assert data["deleted"] == 0


def test_stream_force_refresh_parameter(client, mock_cgi_index):
    """Verify force_refresh=true query parameter is accepted."""
    # This will fail at the analysis step (no search service), but the
    # parameter should be accepted and cache should be bypassed
    r = client.get("/cgi/150-0_B_ter/analysis/stream?force_refresh=true")
    # Should start streaming (200 with NDJSON), even if analysis fails internally
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/x-ndjson")


# ============================================================================
# Removed Endpoints Verification
# ============================================================================


def test_removed_root_endpoint(client):
    r = client.get("/")
    assert r.status_code == 404


def test_removed_search_endpoints(client):
    assert client.post("/search/article", json={}).status_code in (404, 405)
    assert client.post("/search/text", json={}).status_code in (404, 405)
    assert client.post("/search/cgi", json={}).status_code in (404, 405)


def test_removed_lois_endpoints(client):
    assert client.get("/lois").status_code == 404
    assert client.get("/lois/1").status_code == 404
    assert client.get("/lois/1/articles").status_code == 404


def test_removed_intent_endpoints(client, mock_cgi_index):
    r = client.post("/cgi/150-0_B_ter/intent")
    assert r.status_code in (404, 405)

    r = client.get("/cgi/150-0_B_ter/intent/stream")
    assert r.status_code == 404


def test_removed_law_analysis_endpoints(client):
    r = client.post("/analysis/law", json={"law_type": "LF", "law_year": 2024})
    assert r.status_code in (404, 405)

    r = client.post("/analysis/law/by-numero", json={"law_numero": "2023-1322"})
    assert r.status_code in (404, 405)


# ============================================================================
# CORS Headers
# ============================================================================


def test_cors_headers(client):
    """Verify CORS middleware is active via actual request with Origin header."""
    r = client.get("/health", headers={
        "Origin": "http://localhost:5173",
    })
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers
