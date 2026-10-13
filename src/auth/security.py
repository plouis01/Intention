"""Security utilities: password hashing, JWT, OAuth token verification."""

import logging
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import jwt

from src.utils.config import get_settings

logger = logging.getLogger(__name__)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    settings = get_settings()
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.jwt_access_token_expire_minutes)
    )
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: str) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_token_expire_days)
    # jti makes each token unique even when issued within the same second,
    # so rotation produces a new hash and prevents replay.
    to_encode = {
        "sub": user_id,
        "exp": expire,
        "type": "refresh",
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT token. Raises JWTError on failure."""
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])


def verify_google_token(credential: str) -> dict:
    """Verify a Google ID token and return the payload.

    Returns dict with keys: sub, email, name, picture, etc.
    Raises ValueError if verification fails.
    """
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    settings = get_settings()
    if not settings.google_client_id:
        raise ValueError("Google Client ID non configuré")

    try:
        idinfo = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            settings.google_client_id,
        )
        return idinfo
    except Exception as e:
        logger.error(f"Google token verification failed: {e}")
        raise ValueError("Token Google invalide") from e


