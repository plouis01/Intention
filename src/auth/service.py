"""Authentication business logic."""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from src.auth.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_google_token,
    verify_password,
)
from src.db.models import AccountLog, EmailVerificationToken, TrialLink, User, UserSession


def _issue_email_verification_token(db: Session, user: User) -> str:
    """Create a fresh 24h verification token for the user, delete any pending ones."""
    db.query(EmailVerificationToken).filter(
        EmailVerificationToken.user_id == user.id,
        EmailVerificationToken.used == 0,
    ).delete()
    token = str(uuid.uuid4())
    db.add(EmailVerificationToken(
        user_id=user.id,
        token=token,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        used=0,
    ))
    db.commit()
    return token


def _utcnow_naive() -> datetime:
    """Return current UTC time as a naive datetime (for DB comparison)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _log_account_event(db: Session, event: str, provider: str) -> None:
    db.add(AccountLog(event=event, provider=provider, timestamp=datetime.now(timezone.utc)))
    db.commit()


def _hash_token(token: str) -> str:
    """SHA-256 hash of a refresh token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def _build_tokens(
    user: User,
    db: Session,
    device_info: str | None = None,
    ip_address: str | None = None,
    location: str | None = None,
) -> dict:
    """Create access + refresh tokens for a user and persist the session."""
    from src.utils.config import get_settings

    settings = get_settings()
    payload = {
        "sub": user.id,
        "email": user.email,
        "name": user.name,
        "provider": user.provider,
        "role": user.role or "user",
        "email_verified": bool(user.email_verified),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }

    # Add trial expiration claim for trial users (and converted trial users)
    trial_link = db.query(TrialLink).filter(TrialLink.user_id == user.id).first()
    if trial_link and trial_link.expires_at:
        payload["trial_expires_at"] = trial_link.expires_at.isoformat()

    access_token = create_access_token(payload)
    refresh_token = create_refresh_token(user.id)

    # Calculate expiration timestamp in ms (for frontend compatibility)
    expires_at = int(
        (
            datetime.now(timezone.utc).timestamp()
            + settings.jwt_access_token_expire_minutes * 60
        )
        * 1000
    )

    # Persist session - reuse existing session for the same device if present
    now = datetime.now(timezone.utc)
    truncated_device = (device_info or "")[:200] or None
    existing_session = (
        db.query(UserSession)
        .filter(
            UserSession.user_id == user.id,
            UserSession.device_info == truncated_device,
        )
        .first()
    ) if truncated_device else None

    if existing_session:
        existing_session.refresh_token_hash = _hash_token(refresh_token)
        existing_session.ip_address = ip_address
        existing_session.location = location
        existing_session.last_used_at = now
    else:
        db.add(UserSession(
            user_id=user.id,
            refresh_token_hash=_hash_token(refresh_token),
            device_info=truncated_device,
            ip_address=ip_address,
            location=location,
            created_at=now,
            last_used_at=now,
        ))
    db.commit()

    return {
        "token": access_token,
        "expires_at": expires_at,
        "refresh_token": refresh_token,
    }


def redeem_trial_link(
    db: Session,
    token: str,
    device_info: str | None = None,
    ip_address: str | None = None,
    location: str | None = None,
) -> tuple[User, dict]:
    """Redeem a trial link token. Creates ghost user on first visit, re-auths on return."""
    trial_link = db.query(TrialLink).filter(TrialLink.token == token).first()
    if not trial_link:
        raise ValueError("NOT_FOUND")

    if trial_link.status == "revoked":
        raise ValueError("REVOKED")

    # Trial link of a converted account is no longer a magic-link: the user
    # now has a real password and must sign in normally.
    if trial_link.status == "converted":
        raise ValueError("CONVERTED")

    now = _utcnow_naive()

    # Mark expired trials as such for status tracking, but do NOT block re-auth:
    # returning prospects with an expired trial should still log in so the
    # in-app trial-expired banner / paywall can prompt them to subscribe.
    if (
        trial_link.expires_at
        and trial_link.expires_at < now
        and trial_link.status not in ("converted", "expired")
    ):
        trial_link.status = "expired"
        db.commit()

    if trial_link.user_id:
        # Returning prospect - re-authenticate
        user = db.query(User).filter(User.id == trial_link.user_id).first()
        if not user:
            raise ValueError("NOT_FOUND")

        # Debounce: if redeemed within last 60s (e.g. React StrictMode double-mount),
        # reuse existing session instead of creating a duplicate
        if trial_link.redeemed_at and (now - trial_link.redeemed_at).total_seconds() <= 60:
            existing_session = db.query(UserSession).filter(
                UserSession.user_id == user.id
            ).order_by(UserSession.created_at.desc()).first()
            if existing_session:
                from src.utils.config import get_settings
                settings = get_settings()
                payload = {
                    "sub": user.id,
                    "email": user.email,
                    "name": user.name,
                    "provider": user.provider,
                    "role": user.role or "user",
                    "email_verified": bool(user.email_verified),
                    "created_at": user.created_at.isoformat() if user.created_at else None,
                }
                if trial_link.expires_at:
                    payload["trial_expires_at"] = trial_link.expires_at.isoformat()
                access_token = create_access_token(payload)
                expires_at = int(
                    (datetime.now(timezone.utc).timestamp() + settings.jwt_access_token_expire_minutes * 60) * 1000
                )
                return user, {"token": access_token, "expires_at": expires_at, "refresh_token": ""}

        trial_link.visit_count = (trial_link.visit_count or 0) + 1
        trial_link.redeemed_at = now
        db.commit()
        return user, _build_tokens(user, db, device_info, ip_address=ip_address, location=location)

    # First visit - check if email already has a real account
    existing = db.query(User).filter(User.email == trial_link.prospect_email).first()
    if existing and existing.provider != "trial":
        raise ValueError("EMAIL_EXISTS")

    # Activate trial: set expires_at now (timer starts on first open)
    from src.utils.config import get_settings
    settings = get_settings()
    duration = getattr(settings, "trial_duration_days", 7)
    trial_link.expires_at = now + timedelta(days=duration)
    trial_link.status = "active"

    # Create ghost user
    user = User(
        id=str(uuid.uuid4()),
        email=trial_link.prospect_email,
        name=trial_link.prospect_name or trial_link.prospect_email.split("@")[0],
        provider="trial",
        hashed_password=None,
        provider_id=str(trial_link.id),
        role="user",
        email_verified=1,  # Trial link admin-created with validated email
        created_at=now,
    )
    db.add(user)

    trial_link.user_id = user.id
    trial_link.redeemed_at = now
    trial_link.visit_count = 1
    db.commit()
    db.refresh(user)

    _log_account_event(db, "account_created", "trial")

    return user, _build_tokens(user, db, device_info, ip_address=ip_address, location=location)


def register_user(db: Session, email: str, password: str, name: str, device_info: str | None = None, ip_address: str | None = None, location: str | None = None) -> tuple[User, dict]:
    """Register a new user with email/password. Returns (user, tokens)."""
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        if existing.provider == "trial":
            # Transparent upgrade: convert trial ghost to real email account.
            # The admin-created trial link already validated the email address,
            # so mark as verified to skip the extra confirmation step.
            existing.hashed_password = hash_password(password)
            existing.name = name
            existing.provider = "email"
            existing.email_verified = 1
            existing.updated_at = datetime.now(timezone.utc)

            trial_link = db.query(TrialLink).filter(TrialLink.user_id == existing.id).first()
            if trial_link:
                trial_link.status = "converted"
                trial_link.converted_at = datetime.now(timezone.utc)

            db.commit()
            db.refresh(existing)
            return existing, _build_tokens(existing, db, device_info, ip_address=ip_address, location=location)

        if existing.provider == "google":
            raise ValueError("Un compte existe déjà avec cet email. Essayez de vous connecter avec Google.")
        raise ValueError("Cet email est déjà utilisé")

    from src.utils.config import get_settings as _get_settings
    _role = "admin" if email in _get_settings().get_admin_emails() else "user"

    user = User(
        id=str(uuid.uuid4()),
        email=email,
        name=name,
        hashed_password=hash_password(password),
        provider="email",
        role=_role,
        email_verified=0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    _log_account_event(db, "account_created", "email")

    # Verification email replaces the generic welcome email for email signups -
    # the user must confirm before accessing gated routes (enforced server-side
    # by require_active_access).
    token = _issue_email_verification_token(db, user)
    from src.auth.email import send_email_verification_email
    from src.utils.config import get_settings as _gs
    verify_url = f"{_gs().frontend_url}/verify-email?token={token}"
    send_email_verification_email(user.email, verify_url)

    return user, _build_tokens(user, db, device_info, ip_address=ip_address, location=location)


def authenticate_user(db: Session, email: str, password: str, device_info: str | None = None, ip_address: str | None = None, location: str | None = None) -> tuple[User, dict]:
    """Authenticate with email/password. Returns (user, tokens)."""
    user = db.query(User).filter(User.email == email).first()
    if not user or not user.hashed_password:
        raise ValueError("Email ou mot de passe incorrect")

    if not verify_password(password, user.hashed_password):
        raise ValueError("Email ou mot de passe incorrect")

    from src.utils.config import get_settings as _get_settings
    expected_role = "admin" if email in _get_settings().get_admin_emails() else "user"
    if user.role != expected_role:
        user.role = expected_role
        db.commit()
        db.refresh(user)

    return user, _build_tokens(user, db, device_info, ip_address=ip_address, location=location)


def authenticate_google(db: Session, credential: str, device_info: str | None = None, ip_address: str | None = None, location: str | None = None) -> tuple[User, dict]:
    """Authenticate with Google ID token. Creates user if needed."""
    idinfo = verify_google_token(credential)

    email = idinfo.get("email")
    name = idinfo.get("name", email.split("@")[0] if email else "")
    provider_id = idinfo.get("sub")

    if not email:
        raise ValueError("Email non disponible dans le token Google")

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        from src.utils.config import get_settings as _get_settings
        _role = "admin" if email in _get_settings().get_admin_emails() else "user"

        user = User(
            id=str(uuid.uuid4()),
            email=email,
            name=name,
            provider="google",
            provider_id=provider_id,
            role=_role,
            email_verified=1,  # Google OAuth → email trusted
            created_at=datetime.now(timezone.utc),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        _log_account_event(db, "account_created", "google")

        from src.auth.email import send_welcome_email
        send_welcome_email(user.email, user.name)
    elif user.provider == "trial":
        # Transparent upgrade: convert trial ghost to Google account
        user.provider = "google"
        user.provider_id = provider_id
        user.name = name
        user.updated_at = datetime.now(timezone.utc)

        trial_link = db.query(TrialLink).filter(TrialLink.user_id == user.id).first()
        if trial_link:
            trial_link.status = "converted"
            trial_link.converted_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(user)
    else:
        # Existing user - sync role from admin_emails + link Google if needed
        from src.utils.config import get_settings as _get_settings
        expected_role = "admin" if email in _get_settings().get_admin_emails() else "user"
        changed = user.role != expected_role
        if user.provider != "google" and not user.provider_id:
            user.provider_id = provider_id
            changed = True
        if changed:
            user.role = expected_role
            user.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(user)

    return user, _build_tokens(user, db, device_info, ip_address=ip_address, location=location)


def refresh_tokens(db: Session, refresh_token: str, device_info: str | None = None, ip_address: str | None = None, location: str | None = None) -> tuple[User, dict]:
    """Validate refresh token and issue new tokens."""
    try:
        payload = decode_token(refresh_token)
    except Exception:
        raise ValueError("Refresh token invalide ou expiré")

    if payload.get("type") != "refresh":
        raise ValueError("Token invalide")

    user_id = payload.get("sub")
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise ValueError("Utilisateur non trouvé")

    # Validate session exists (revocation check).
    # SELECT ... FOR UPDATE serializes concurrent /auth/refresh calls on the
    # same refresh token (e.g. two browser tabs racing). Without the lock,
    # one transaction sees the session AFTER the other deletes it, yielding
    # a spurious 401 "Session révoquée" — or worse, both succeed and leave
    # an orphan session in the DB. (No-op on SQLite used in tests.)
    old_hash = _hash_token(refresh_token)
    old_session = (
        db.query(UserSession)
        .filter(UserSession.refresh_token_hash == old_hash)
        .with_for_update()
        .first()
    )
    if not old_session:
        raise ValueError("Session révoquée")

    # Capture old session info before deleting
    old_device_info = old_session.device_info
    old_ip = old_session.ip_address
    old_location = old_session.location

    # Delete old session (new one created by _build_tokens)
    db.delete(old_session)
    db.flush()

    return user, _build_tokens(
        user, db,
        device_info or old_device_info,
        ip_address=ip_address or old_ip,
        location=location or old_location,
    )


def check_trial_active(user: User, db: Session) -> None:
    """Check if a trial user's access is still active. Raises ValueError if expired/revoked."""
    if user.role == "admin":
        return

    trial_link = db.query(TrialLink).filter(TrialLink.user_id == user.id).first()
    if not trial_link:
        # Not a trial user (no trial link) - allow access
        return

    if trial_link.status == "revoked":
        raise ValueError("TRIAL_REVOKED")

    # Trial activated but expired
    now = _utcnow_naive()
    if trial_link.expires_at and trial_link.expires_at < now:
        if trial_link.status == "active":
            trial_link.status = "expired"
            db.commit()
        raise ValueError("TRIAL_EXPIRED")
