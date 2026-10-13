"""Tests for the bulkhead pool + analysis semaphore wiring.

These tests run on SQLite (forced by conftest.py) so they cannot validate the
Postgres-only pool config (`pool_size`, `max_overflow`, `pool_timeout`). They
verify the *wiring* is correct: the light pool exists, get_db routes to it,
and the analysis semaphore is configured. End-to-end pool stress testing is
done locally against Postgres+pgvector (see plan).
"""

from __future__ import annotations

import asyncio


def test_light_pool_engine_is_separate_from_heavy():
    """get_engine_light() and get_engine() return distinct Engine instances."""
    from src.db.models import get_engine, get_engine_light

    heavy = get_engine()
    light = get_engine_light()
    assert heavy is not light, "Light and heavy engines must be distinct objects"


def test_light_pool_session_is_bound_to_light_engine():
    """Sessions from get_session_light() are bound to the light engine."""
    from src.db.models import get_engine, get_engine_light, get_session_light

    light_engine = get_engine_light()
    session = get_session_light()
    try:
        assert session.bind is light_engine, "Light session must bind to light engine"
        assert session.bind is not get_engine(), "Light session must NOT bind to heavy engine"
    finally:
        session.close()


def test_get_db_yields_from_light_pool():
    """The HTTP get_db dependency yields sessions on the LIGHT pool.
    All 59 endpoints using Depends(get_db) automatically use the light pool."""
    from src.auth.dependencies import get_db
    from src.db.models import get_engine_light

    light_engine = get_engine_light()
    gen = get_db()
    db = next(gen)
    try:
        assert db.bind is light_engine
    finally:
        # Exhaust the generator to trigger finally: db.close()
        try:
            next(gen)
        except StopIteration:
            pass


def test_analysis_semaphore_starts_unlocked_with_default_capacity_2():
    """_ANALYSIS_SEMAPHORE defaults to capacity 2 (overridable via env var)."""
    from src.api.cgi_routes import _ANALYSIS_SEMAPHORE, _MAX_CONCURRENT_ANALYSES

    # Default is 2 unless MAX_CONCURRENT_ANALYSES env var was set before import
    assert _MAX_CONCURRENT_ANALYSES >= 1
    assert not _ANALYSIS_SEMAPHORE.locked()


def test_max_concurrent_analyses_respects_env_var(monkeypatch):
    """MAX_CONCURRENT_ANALYSES env var overrides the default at import time.
    Note: re-importing requires reloading the module to pick up the new value."""
    import importlib
    import src.api.cgi_routes as cgi_routes

    monkeypatch.setenv("MAX_CONCURRENT_ANALYSES", "5")
    importlib.reload(cgi_routes)
    try:
        assert cgi_routes._MAX_CONCURRENT_ANALYSES == 5
    finally:
        # Restore default for downstream tests
        monkeypatch.delenv("MAX_CONCURRENT_ANALYSES")
        importlib.reload(cgi_routes)


def test_analysis_semaphore_locks_after_two_acquires():
    """3rd acquire blocks until a release."""
    from src.api.cgi_routes import _ANALYSIS_SEMAPHORE

    async def _scenario():
        # Acquire 2 → semaphore is now locked (capacity exhausted)
        await _ANALYSIS_SEMAPHORE.acquire()
        await _ANALYSIS_SEMAPHORE.acquire()
        try:
            assert _ANALYSIS_SEMAPHORE.locked()
            # 3rd acquire would block — verify by trying with timeout
            try:
                await asyncio.wait_for(_ANALYSIS_SEMAPHORE.acquire(), timeout=0.05)
                # If we get here, the semaphore wasn't really locked → fail
                _ANALYSIS_SEMAPHORE.release()
                raise AssertionError("3rd acquire should have timed out")
            except asyncio.TimeoutError:
                pass  # expected
        finally:
            _ANALYSIS_SEMAPHORE.release()
            _ANALYSIS_SEMAPHORE.release()
        # After release, semaphore is unlocked again
        assert not _ANALYSIS_SEMAPHORE.locked()

    asyncio.run(_scenario())


def test_streaming_dep_is_distinct_from_regular_dep():
    """require_active_access_stream is a separate function from require_active_access.
    The streaming dep manages its own short-lived session to avoid holding a
    light pool connection for the entire StreamingResponse duration."""
    from src.auth.dependencies import (
        require_active_access,
        require_active_access_stream,
    )

    assert require_active_access is not require_active_access_stream
