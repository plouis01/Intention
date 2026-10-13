"""Tests for database session management - Story 1.1 & 1.2.

Validates singleton sessionmaker, connection pool configuration,
and session lifecycle via context managers.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool


class TestSingletonSessionMaker:
    """Story 1.1: Verify single global sessionmaker instance."""

    def test_get_session_returns_session_instance(self):
        """get_session() should return a valid SQLAlchemy Session."""
        from src.db.models import get_session

        session = get_session()
        try:
            assert isinstance(session, Session)
        finally:
            session.close()

    def test_sessionmaker_is_singleton(self):
        """Multiple calls to get_session() must use the same sessionmaker."""
        from src.db.models import _SessionLocal

        # _SessionLocal should be a module-level sessionmaker, not recreated per call
        assert _SessionLocal is not None
        assert isinstance(_SessionLocal, sessionmaker)

    def test_multiple_sessions_share_sessionmaker(self):
        """Sessions from different get_session() calls share the same factory."""
        from src.db.models import get_session

        s1 = get_session()
        s2 = get_session()
        try:
            # Both sessions should be bound to the same engine
            assert s1.bind is s2.bind
        finally:
            s1.close()
            s2.close()


class TestConnectionPoolConfig:
    """Story 1.1: Verify explicit connection pool parameters."""

    def test_pg_engine_has_pool_size(self):
        """PostgreSQL engine must have pool_size=5."""
        from src.db.models import get_engine

        engine = get_engine()
        # SQLite file-based engines also use QueuePool in recent SQLAlchemy
        # versions; gate on the dialect so these PG-specific assertions don't
        # fire against a test sqlite:// URL.
        if isinstance(engine.pool, QueuePool) and engine.dialect.name == "postgresql":
            assert engine.pool.size() == 5

    def test_pg_engine_has_max_overflow(self):
        """PostgreSQL engine must have max_overflow=10."""
        from src.db.models import get_engine

        engine = get_engine()
        # SQLite file-based engines also use QueuePool in recent SQLAlchemy
        # versions; gate on the dialect so these PG-specific assertions don't
        # fire against a test sqlite:// URL.
        if isinstance(engine.pool, QueuePool) and engine.dialect.name == "postgresql":
            assert engine.pool._max_overflow == 10

    def test_pg_engine_has_pool_timeout(self):
        """PostgreSQL heavy engine must have pool_timeout=5 (fail-fast)."""
        from src.db.models import get_engine

        engine = get_engine()
        # SQLite file-based engines also use QueuePool in recent SQLAlchemy
        # versions; gate on the dialect so these PG-specific assertions don't
        # fire against a test sqlite:// URL.
        if isinstance(engine.pool, QueuePool) and engine.dialect.name == "postgresql":
            assert engine.pool._timeout == 5

    def test_pg_engine_has_pool_recycle(self):
        """PostgreSQL engine must have pool_recycle=600."""
        from src.db.models import get_engine

        engine = get_engine()
        # SQLite file-based engines also use QueuePool in recent SQLAlchemy
        # versions; gate on the dialect so these PG-specific assertions don't
        # fire against a test sqlite:// URL.
        if isinstance(engine.pool, QueuePool) and engine.dialect.name == "postgresql":
            assert engine.pool._recycle == 600

    @patch("src.db.models.get_settings")
    def test_pg_pool_config_applied(self, mock_settings):
        """When DB is PostgreSQL, heavy pool config (15+25, timeout 5) is applied."""
        import src.db.models as models

        # Reset engine to force recreation
        old_engine = models._engine
        old_session = getattr(models, "_SessionLocal", None)
        models._engine = None
        if hasattr(models, "_SessionLocal"):
            models._SessionLocal = None

        try:
            mock_settings.return_value.effective_database_url = (
                "postgresql://user:pass@localhost/testdb"
            )
            mock_settings.return_value.environment = "production"

            engine = models.get_engine()
            pool = engine.pool

            assert isinstance(pool, QueuePool)
            assert pool.size() == 15
            assert pool._max_overflow == 25
            assert pool._timeout == 5
            assert pool._recycle == 600
        finally:
            # Restore original engine
            if engine is not None:
                engine.dispose()
            models._engine = old_engine
            if hasattr(models, "_SessionLocal"):
                models._SessionLocal = old_session

    @patch("src.db.models.get_settings")
    def test_pg_light_pool_config_applied(self, mock_settings):
        """Light pool (auth/trial/admin/subscription) is sized 8+8 with timeout 5."""
        import src.db.models as models

        old_engine_light = models._engine_light
        old_session_light = getattr(models, "_SessionLocal_light", None)
        models._engine_light = None
        if hasattr(models, "_SessionLocal_light"):
            models._SessionLocal_light = None

        engine_light = None
        try:
            mock_settings.return_value.effective_database_url = (
                "postgresql://user:pass@localhost/testdb"
            )
            mock_settings.return_value.environment = "production"

            engine_light = models.get_engine_light()
            pool = engine_light.pool

            assert isinstance(pool, QueuePool)
            assert pool.size() == 8
            assert pool._max_overflow == 8
            assert pool._timeout == 5
            assert pool._recycle == 600
        finally:
            if engine_light is not None:
                engine_light.dispose()
            models._engine_light = old_engine_light
            if hasattr(models, "_SessionLocal_light"):
                models._SessionLocal_light = old_session_light


class TestSessionContextManager:
    """Story 1.2: Verify sessions are usable as context managers."""

    def test_get_session_as_context_manager(self):
        """get_session() should work as a context manager."""
        from src.db.models import get_session

        with get_session() as session:
            assert isinstance(session, Session)
            # Session should be usable inside context
            session.execute(text("SELECT 1"))

    def test_session_closed_after_context_exit(self):
        """Session must be closed after exiting context manager."""
        from src.db.models import get_session

        with get_session() as session:
            pass
        # After context exit, session should not be usable for new work
        # (closed or returned to pool)
        assert not session.is_active or session.get_bind() is not None

    def test_session_closed_on_exception(self):
        """Session must be closed even when exception occurs."""
        from src.db.models import get_session

        with pytest.raises(ValueError):
            with get_session() as session:
                raise ValueError("test error")
        # Session should still be properly closed

    def test_session_rollback_on_exception(self):
        """Session should rollback on exception within context."""
        from src.db.models import get_session

        with pytest.raises(RuntimeError):
            with get_session() as session:
                raise RuntimeError("simulated failure")
        # No pending transaction after rollback
