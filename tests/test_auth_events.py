"""Tests for PR 5 - structured auth events audit log."""

import os
import uuid

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-" + uuid.uuid4().hex)
os.environ.setdefault("ENVIRONMENT", "development")

from src.api.main import app  # noqa: E402
from src.db.models import AuthEvent, get_session, init_db, reset_db  # noqa: E402
from src.utils.rate_limit import limiter  # noqa: E402


@pytest.fixture
def client():
    reset_db()
    init_db()
    limiter.reset()
    return TestClient(app)


def _events(event_name: str):
    db = get_session()
    try:
        return db.query(AuthEvent).filter(AuthEvent.event == event_name).all()
    finally:
        db.close()


def test_signup_logs_signup_success(client):
    email = f"u-{uuid.uuid4().hex[:6]}@example.com"
    res = client.post("/auth/signup", json={"email": email, "password": "password123", "name": "X"})
    assert res.status_code == 200
    rows = _events("signup_success")
    assert len(rows) == 1


def test_login_logs_success_and_failure(client):
    email = f"u-{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/signup", json={"email": email, "password": "password123", "name": "X"})

    # Bad password → login_failed
    res = client.post("/auth/login", json={"email": email, "password": "wrong"})
    assert res.status_code == 401
    assert len(_events("login_failed")) == 1

    # Good → login_success
    res = client.post("/auth/login", json={"email": email, "password": "password123"})
    assert res.status_code == 200
    assert len(_events("login_success")) == 1


def test_logout_logs_event(client):
    email = f"u-{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/signup", json={"email": email, "password": "password123", "name": "X"})
    client.post("/auth/logout")
    assert len(_events("logout")) == 1


def test_events_have_ip_and_user_agent(client):
    email = f"u-{uuid.uuid4().hex[:6]}@example.com"
    client.post(
        "/auth/signup",
        json={"email": email, "password": "password123", "name": "X"},
        headers={"User-Agent": "PyTestAgent/1.0"},
    )
    rows = _events("signup_success")
    assert rows[0].user_agent == "PyTestAgent/1.0"
    assert rows[0].ip_address is not None
