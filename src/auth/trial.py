"""Trial link API routes."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from src.auth.dependencies import get_current_user, get_db
from src.auth.geolocation import resolve_location
from src.auth.routes import _set_refresh_cookie, _split_tokens
from src.auth.scanner_detection import classify_request
from src.auth.schemas import AuthResponse, RedeemTrialRequest, TokenResponse, TrialInspectResponse, TrialStatusResponse, UserResponse
from src.auth.service import redeem_trial_link
from src.db.models import TrialLink, TrialLinkVisit, User
from src.utils.audit import log_auth_event
from src.utils.config import get_settings
from src.utils.rate_limit import limiter

logger = logging.getLogger(__name__)

trial_router = APIRouter(prefix="/trial", tags=["trial"])


def _log_trial_visit(db: Session, trial_link_id: int, event_type: str, request: Request) -> tuple[str, str | None]:
    """Persist a TrialLinkVisit row classified as human / bot / unknown.

    Failures are swallowed: visit logging must never break the redeem flow.
    """
    classification, scanner_name = classify_request(request)
    try:
        visit = TrialLinkVisit(
            trial_link_id=trial_link_id,
            event_type=event_type,
            classification=classification,
            scanner_name=scanner_name,
            ip_address=request.client.host if request.client else None,
            user_agent=(request.headers.get("User-Agent") or "")[:300] or None,
            accept_language=(request.headers.get("Accept-Language") or "")[:100] or None,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(visit)
        db.commit()
    except Exception as e:
        logger.warning(f"Failed to log trial visit: {e}")
        db.rollback()
    return classification, scanner_name


def _to_user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        provider=user.provider,
        role=user.role,
        email_verified=bool(user.email_verified),
        created_at=user.created_at.isoformat() if user.created_at else "",
    )


def _to_auth_response(user: User, tokens: dict) -> AuthResponse:
    return AuthResponse(
        user=_to_user_response(user),
        token=TokenResponse(**tokens),
    )


_ERROR_MAP = {
    "NOT_FOUND": (404, "Lien d'essai invalide"),
    "REVOKED": (410, "Ce lien d'essai a été révoqué"),
    "EXPIRED": (410, "Ce lien d'essai a expiré"),
    "LINK_EXPIRED": (410, "Ce lien d'essai n'est plus valide. Veuillez en demander un nouveau."),
    "EMAIL_EXISTS": (409, "Un compte existe déjà avec cet email. Connectez-vous normalement."),
    "CONVERTED": (410, "Votre compte a déjà été converti. Connectez-vous avec votre mot de passe."),
}


@trial_router.get("/inspect", response_model=TrialInspectResponse)
@limiter.limit("20/minute")
def inspect_trial(request: Request, token: str, db: Session = Depends(get_db)):
    """Return prospect info for a trial token without consuming it.

    Side-effect: logs the hit (classified as human / bot / unknown) so admins
    can see when scanners pre-fetch links vs when the prospect actually visits.
    """
    trial_link = db.query(TrialLink).filter(TrialLink.token == token).first()
    if not trial_link:
        raise HTTPException(status_code=404, detail="Lien d'essai invalide")

    _log_trial_visit(db, trial_link.id, "inspect", request)

    settings = get_settings()
    return TrialInspectResponse(
        prospect_email=trial_link.prospect_email,
        prospect_name=trial_link.prospect_name,
        link_expires_at=trial_link.link_expires_at.isoformat(),
        trial_duration_days=settings.trial_duration_days,
        already_activated=trial_link.user_id is not None,
        is_converted=trial_link.status == "converted",
    )


@trial_router.post("/redeem", response_model=AuthResponse)
@limiter.limit("10/minute")
def redeem_trial(request: Request, data: RedeemTrialRequest, response: Response, db: Session = Depends(get_db)):
    try:
        # Log the redeem attempt before activation so we capture scanner-driven
        # POSTs even if redemption succeeds. Lookup is cheap (token is indexed).
        trial_link = db.query(TrialLink).filter(TrialLink.token == data.token).first()
        if trial_link:
            _log_trial_visit(db, trial_link.id, "redeem", request)

        ua = request.headers.get("User-Agent")
        ip = request.client.host if request.client else None
        location = resolve_location(ip)
        user, tokens = redeem_trial_link(db, data.token, device_info=ua, ip_address=ip, location=location)
        response_tokens, refresh_token = _split_tokens(tokens)
        _set_refresh_cookie(request, response, refresh_token)
        log_auth_event(db, event="trial_redeemed", user_id=user.id, request=request)
        return _to_auth_response(user, response_tokens)
    except ValueError as e:
        code = str(e)
        status_code, detail = _ERROR_MAP.get(code, (400, str(e)))
        raise HTTPException(status_code=status_code, detail=detail)


@trial_router.get("/status", response_model=TrialStatusResponse)
def trial_status(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    trial_link = db.query(TrialLink).filter(TrialLink.user_id == current_user.id).first()
    if not trial_link:
        return TrialStatusResponse(
            is_trial=False,
            days_remaining=0,
            expires_at=None,
            is_expired=False,
            is_revoked=False,
            can_search=True,
        )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    is_revoked = trial_link.status == "revoked"

    # Trial not yet activated (should not happen - user_id is set on redemption
    # which also sets expires_at, but guard defensively)
    if not trial_link.expires_at:
        return TrialStatusResponse(
            is_trial=True,
            days_remaining=0,
            expires_at=None,
            is_expired=False,
            is_revoked=is_revoked,
            can_search=not is_revoked,
        )

    is_expired = trial_link.expires_at < now
    # Use ceil (not timedelta.days which truncates) so the banner shows the
    # intuitive value: "7 jours restants" just after a 7-day trial is issued,
    # not "6" once a minute has elapsed. Also matches the frontend fast-path
    # (useTrialStatus reads from the JWT claim with Math.ceil) - otherwise
    # the value flickers between back and front.
    if is_expired:
        days_remaining = 0
    else:
        total_seconds = (trial_link.expires_at - now).total_seconds()
        days_remaining = max(0, -(-int(total_seconds) // 86400))  # ceil division

    return TrialStatusResponse(
        is_trial=True,
        days_remaining=days_remaining,
        expires_at=trial_link.expires_at.isoformat(),
        is_expired=is_expired,
        is_revoked=is_revoked,
        can_search=not is_expired and not is_revoked,
    )
