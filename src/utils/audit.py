"""Audit-log helper for structured auth / profile events.

Rows live in the `auth_events` table and are purged after 90 days by the
startup task in src.api.main. Prefer this helper over ad-hoc logger calls so
events stay machine-queryable (event names are stable identifiers).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from src.db.models import AuthEvent

logger = logging.getLogger(__name__)


def log_auth_event(
    db: Session,
    *,
    event: str,
    user_id: str | None = None,
    request: Request | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Insert a row in auth_events. Swallows errors so audit issues never
    break the real request path."""
    try:
        ua = None
        ip = None
        if request is not None:
            ua_header = request.headers.get("User-Agent")
            ua = (ua_header or "")[:200] or None
            ip = request.client.host if request.client else None
        db.add(AuthEvent(
            user_id=user_id,
            event=event,
            ip_address=ip,
            user_agent=ua,
            extra_data=json.dumps(extra) if extra else None,
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()
    except Exception as e:
        logger.warning("log_auth_event failed (%s): %s", event, e)
