"""Tests for auth/trial endpoint rate limiting (slowapi)."""

import os
import uuid

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-" + uuid.uuid4().hex)
os.environ.setdefault("ENVIRONMENT", "development")

from src.api.main import app  # noqa: E402
from src.db.models import init_db, reset_db  # noqa: E402
from src.utils.rate_limit import limiter  # noqa: E402


@pytest.fixture
def client():
    reset_db()
    init_db()
    limiter.reset()
    return TestClient(app)


def test_login_rate_limit(client):
    # Limit is 5/minute - the 6th call must return 429.
    for i in range(5):
        res = client.post("/auth/login", json={"email": f"x{i}@x.com", "password": "nope"})
        # 401 (bad creds) is expected, not 429
        assert res.status_code in (401, 400), res.status_code
    res = client.post("/auth/login", json={"email": "x@x.com", "password": "nope"})
    assert res.status_code == 429
    assert "Trop de requêtes" in res.json().get("detail", "")


def test_signup_rate_limit(client):
    # Limit is 10/minute
    for _ in range(10):
        email = f"user-{uuid.uuid4().hex[:8]}@example.com"
        res = client.post("/auth/signup", json={"email": email, "password": "password123", "name": "x"})
        assert res.status_code == 200
    email = f"user-{uuid.uuid4().hex[:8]}@example.com"
    res = client.post("/auth/signup", json={"email": email, "password": "password123", "name": "x"})
    assert res.status_code == 429


def test_forgot_password_rate_limit(client):
    for _ in range(3):
        res = client.post("/auth/forgot-password", json={"email": "whatever@example.com"})
        assert res.status_code == 200
    res = client.post("/auth/forgot-password", json={"email": "whatever@example.com"})
    assert res.status_code == 429


def test_limiter_reset_between_tests(client):
    # Sanity: fixture resets the limiter so this test starts fresh.
    res = client.post("/auth/login", json={"email": "x@x.com", "password": "nope"})
    assert res.status_code in (401, 400)
