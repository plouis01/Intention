"""Tests for API endpoints."""

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.db.models import init_db


@pytest.fixture
def client():
    """Create test client."""
    init_db()
    return TestClient(app)


def test_health(client):
    """Test health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "embedding_provider" in data
    assert "llm_provider" in data


def test_openapi_docs(client):
    """Test OpenAPI documentation is available."""
    response = client.get("/docs")
    assert response.status_code == 200

    response = client.get("/openapi.json")
    assert response.status_code == 200
    data = response.json()
    assert data["info"]["title"] == "Intention du Législateur API"


def test_removed_endpoints_return_404(client):
    """Removed endpoints should return 404 or 405."""
    # Root endpoint removed
    response = client.get("/")
    assert response.status_code == 404

    # Search endpoints removed
    response = client.post("/search/article", json={})
    assert response.status_code in (404, 405)

    response = client.post("/search/text", json={"query": "test"})
    assert response.status_code in (404, 405)

    response = client.post("/search/cgi", json={"article_ref": "test"})
    assert response.status_code in (404, 405)

    # Lois endpoints removed
    response = client.get("/lois")
    assert response.status_code == 404

    response = client.get("/lois/1")
    assert response.status_code == 404

    # Law analysis endpoints removed
    response = client.post("/analysis/law", json={"law_type": "LF", "law_year": 2024})
    assert response.status_code in (404, 405)
