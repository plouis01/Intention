"""FastAPI dependencies for authentication."""

import os

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from src.auth.security import decode_token
from src.auth.service import check_trial_active
from src.db.models import Subscription, User, get_session_light

_LOCALHOST_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _local_dev_bypass_user(request: Request | None) -> User | None:
    """When DEV_ALLOW_LOCAL_UNAUTH=1 is set AND the request originates from
    loopback, return a synthetic admin user that satisfies downstream checks.
    Used exclusively by scripts/benchmark_analysis.py and other local tooling.
    Returns None otherwise (caller falls through to normal auth)."""
    if os.environ.get("DEV_ALLOW_LOCAL_UNAUTH") != "1":
        return None
    if request is None or request.client is None:
        return None
    if request.client.host not in _LOCALHOST_HOSTS:
        return None
    u = User(
        id="dev-bypass",
        email="dev-bypass@localhost",
        role="admin",
        provider="dev",
        email_verified=1,
    )
    return u

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def get_db():
    """Yield a database session from the LIGHT pool.
    Used by all HTTP routes (auth/trial/admin/subscription).
    Heavy work (analyses, search, workers) uses get_session() directly,
    which routes to a separate pool that cannot starve auth queries."""
    db = get_session_light()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Extract and validate JWT, return the User. Raises 401 if invalid."""
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentification requise",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token invalide",
            )
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token invalide",
            )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalide ou expiré",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Utilisateur non trouvé",
        )
    return user


def get_admin_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Like get_current_user but requires admin role. Raises 403 if not admin."""
    user = get_current_user(token=token, db=db)
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accès réservé aux administrateurs",
        )
    return user


def require_active_access(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Require user to have active subscription or active trial. Raises 403 otherwise."""
    user = get_current_user(token=token, db=db)

    # Admins always have access
    if user.role == "admin":
        return user

    # Email verification required for email-provider users
    if user.provider == "email" and not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email_not_verified",
        )

    # Check for active paid subscription (past_due gets grace period - Stripe retries payment)
    subscription = db.query(Subscription).filter(Subscription.user_id == user.id).first()
    if subscription and subscription.status in ("active", "past_due"):
        return user

    # Check for active trial
    try:
        check_trial_active(user, db)
        return user
    except ValueError as e:
        code = str(e)
        if code == "TRIAL_EXPIRED":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="trial_expired",
            )
        elif code == "TRIAL_REVOKED":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="trial_revoked",
            )

    # No subscription, no trial
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="subscription_required",
    )


def get_optional_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User | None:
    """Like get_current_user but returns None if no token provided."""
    if token is None:
        return None
    try:
        return get_current_user(token=token, db=db)
    except HTTPException:
        return None


def require_active_access_stream(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
) -> User:
    """Variant of require_active_access for streaming endpoints.

    Manages its own short-lived session (open → check → close) instead of
    using Depends(get_db). Without this, FastAPI would keep the yielded
    session open for the entire StreamingResponse lifetime (30-60s for an
    analysis), holding 1 light pool connection. With this dep, the connection
    is returned to the pool as soon as the auth check completes, BEFORE the
    stream begins.

    The returned User is detached (expunged) from the session — its base
    attributes (id, email, role, etc.) remain accessible, but lazy-loaded
    relationships are not. Callers must not access user.subscriptions etc.
    """
    bypass = _local_dev_bypass_user(request)
    if bypass is not None:
        return bypass
    from src.db.models import get_session_light
    db = get_session_light()
    try:
        user = get_current_user(token=token, db=db)

        if user.role == "admin":
            db.expunge(user)
            return user

        if user.provider == "email" and not user.email_verified:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="email_not_verified",
            )

        subscription = db.query(Subscription).filter(Subscription.user_id == user.id).first()
        if subscription and subscription.status in ("active", "past_due"):
            db.expunge(user)
            return user

        try:
            check_trial_active(user, db)
            db.expunge(user)
            return user
        except ValueError as e:
            code = str(e)
            if code == "TRIAL_EXPIRED":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="trial_expired",
                )
            elif code == "TRIAL_REVOKED":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="trial_revoked",
                )

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="subscription_required",
        )
    finally:
        db.close()
