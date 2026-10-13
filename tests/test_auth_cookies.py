"""Tests for cookie-based auth flow: refresh cookie issuance, refresh via cookie, logout."""

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


@pytest.fixture
def signed_up(client):
    email = f"user-{uuid.uuid4().hex[:8]}@example.com"
    password = "password123"
    name = "Cookie User"
    res = client.post("/auth/signup", json={"email": email, "password": password, "name": name})
    assert res.status_code == 200, res.text
    return email, password, res


def test_signup_sets_refresh_cookie_and_omits_it_from_body(signed_up):
    _, _, res = signed_up
    # Body must not include refresh_token
    body = res.json()
    assert "refresh_token" not in body["token"]
    assert body["token"]["token"]
    assert body["token"]["expires_at"]
    # Cookie must be set with the right attributes
    set_cookie = res.headers.get("set-cookie", "")
    assert "refresh_token=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Path=/auth" in set_cookie
    assert "samesite=strict" in set_cookie.lower()
    # TestClient uses http://testserver → no Secure flag (browsers reject
    # Secure cookies on plain HTTP, breaking local dev).
    assert "Secure" not in set_cookie


def test_secure_flag_added_when_behind_https_proxy(client):
    email = f"user-{uuid.uuid4().hex[:8]}@example.com"
    res = client.post(
        "/auth/signup",
        json={"email": email, "password": "password123", "name": "Proxy"},
        headers={"x-forwarded-proto": "https"},
    )
    assert res.status_code == 200
    assert "Secure" in res.headers.get("set-cookie", "")


def test_login_sets_refresh_cookie(client, signed_up):
    email, password, _ = signed_up
    res = client.post("/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200
    assert "refresh_token=" in res.headers.get("set-cookie", "")
    assert "refresh_token" not in res.json()["token"]


def test_refresh_via_cookie_rotates_token(client, signed_up):
    # The TestClient stores cookies from the last signup response
    first = client.cookies.get("refresh_token")
    assert first
    res = client.post("/auth/refresh")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["token"]["token"]
    # New cookie should replace the old one (rotation)
    second = client.cookies.get("refresh_token")
    assert second and second != first


def test_refresh_without_cookie_returns_401(client):
    res = client.post("/auth/refresh")
    assert res.status_code == 401


def test_logout_clears_cookie_and_revokes_session(client, signed_up):
    assert client.cookies.get("refresh_token")
    res = client.post("/auth/logout")
    assert res.status_code == 200
    # After logout, the refresh cookie should be cleared; TestClient reflects
    # the Set-Cookie Max-Age=0 by dropping the cookie on the next request.
    res = client.post("/auth/refresh")
    assert res.status_code == 401


def test_logout_is_idempotent(client):
    # No cookie at all
    res = client.post("/auth/logout")
    assert res.status_code == 200


# ─── Sessions / "Appareils connectés" ──────────────────────────────


def _bearer(client):
    """Extract a Bearer header for the most recently signed-up user."""
    # Re-fetch a fresh access token via the cookie we already have.
    res = client.post("/auth/refresh")
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['token']['token']}"}


def test_get_sessions_returns_current_session(client, signed_up):
    headers = _bearer(client)
    res = client.get("/auth/sessions", headers=headers)
    assert res.status_code == 200
    sessions = res.json()
    assert isinstance(sessions, list)
    assert len(sessions) >= 1
    # At least one session must be flagged as current (the one tied to our cookie)
    assert any(s["is_current"] for s in sessions)


def test_revoke_specific_session_removes_it(client, signed_up):
    # Create a 2nd session by simulating a different device (different UA)
    email, password, _ = signed_up
    other = TestClient(app)
    res = other.post(
        "/auth/login",
        json={"email": email, "password": password},
        headers={"User-Agent": "OtherBrowser/1.0"},
    )
    assert res.status_code == 200

    headers = _bearer(client)
    sessions = client.get("/auth/sessions", headers=headers).json()
    assert len(sessions) == 2
    target = next(s for s in sessions if not s["is_current"])

    res = client.delete(f"/auth/sessions/{target['id']}", headers=headers)
    assert res.status_code == 200
    assert res.json() == {"ok": True}

    sessions = client.get("/auth/sessions", headers=headers).json()
    assert len(sessions) == 1
    assert sessions[0]["is_current"]


def test_revoke_already_deleted_session_returns_404(client, signed_up):
    """Race condition: 2nd revoke of the same id returns 404 cleanly."""
    headers = _bearer(client)
    # Use a non-existent id — same code path as racing with a prior delete
    res = client.delete("/auth/sessions/999999", headers=headers)
    assert res.status_code == 404


def test_revoke_all_keeps_current_session(client, signed_up):
    email, password, _ = signed_up
    other = TestClient(app)
    res = other.post(
        "/auth/login",
        json={"email": email, "password": password},
        headers={"User-Agent": "OtherBrowser/1.0"},
    )
    assert res.status_code == 200

    headers = _bearer(client)
    assert len(client.get("/auth/sessions", headers=headers).json()) == 2

    res = client.delete("/auth/sessions", headers=headers)
    assert res.status_code == 200

    remaining = client.get("/auth/sessions", headers=headers).json()
    assert len(remaining) == 1
    assert remaining[0]["is_current"]


def test_revoke_all_without_cookie_returns_400(client, signed_up):
    """M5: Without the refresh_token cookie we must reject, otherwise we'd
    delete the caller's own session and silently log them out."""
    headers = _bearer(client)
    # Drop the cookie before the DELETE call
    client.cookies.delete("refresh_token")
    res = client.delete("/auth/sessions", headers=headers)
    assert res.status_code == 400
    assert res.json()["detail"] == "missing_refresh_cookie"


def test_revoke_other_user_session_returns_404(client, signed_up):
    """A user must not be able to delete another user's session."""
    # Sign up a 2nd user from a fresh client
    other = TestClient(app)
    other_email = f"other-{uuid.uuid4().hex[:8]}@example.com"
    res = other.post(
        "/auth/signup",
        json={"email": other_email, "password": "password123", "name": "Other"},
    )
    assert res.status_code == 200
    other_token = res.json()["token"]["token"]

    # Get other user's session id (via THEIR own GET /sessions)
    other_sessions = other.get(
        "/auth/sessions", headers={"Authorization": f"Bearer {other_token}"}
    ).json()
    other_session_id = other_sessions[0]["id"]

    # Try to delete it as the first user — should 404 (filter by user_id)
    headers = _bearer(client)
    res = client.delete(f"/auth/sessions/{other_session_id}", headers=headers)
    assert res.status_code == 404
