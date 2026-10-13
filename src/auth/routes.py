"""Authentication API routes."""

import logging
import os
import re
import uuid

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from src.auth.dependencies import get_current_user, get_db
from src.auth.schemas import (
    AddHistoryRequest,
    AuthResponse,
    ChangePasswordRequest,
    ContactRequest,
    FeedbackRequest,
    MessageRequest,
    ForgotPasswordRequest,
    GoogleAuthRequest,
    LoginRequest,
    ReportArticleRequest,
    RequestEmailChangeRequest,
    ResetPasswordRequest,
    SearchHistoryEntry,
    SearchHistoryResponse,
    SignUpRequest,
    TokenResponse,
    UserResponse,
    VerifyEmailChangeRequest,
    VerifyEmailRequest,
)
from src.utils.audit import log_auth_event
from src.utils.config import get_settings
from src.utils.rate_limit import limiter
from src.auth.geolocation import resolve_location
from src.auth.service import (
    authenticate_google,
    authenticate_user,
    refresh_tokens,
    register_user,
)
from src.auth.service import _hash_token
from src.db.models import AccountLog, AnalysisFeedback, ArticleReport, ContactMessage, ConversationMessage, EmailChangeToken, EmailVerificationToken, Notification, PasswordResetToken, SearchHistory, Subscription, TrialLink, User, UserSession

logger = logging.getLogger(__name__)

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "uploads")
ALLOWED_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf"}
ALLOWED_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "pdf"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


def _save_upload(file: UploadFile) -> str:
    """Validate and save an uploaded file, return the filename."""
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=400, detail="Type de fichier non autorisé")
    ext = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else ""
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail="Extension de fichier non autorisée")
    # Read in chunks to avoid loading huge files into memory
    content = bytearray()
    while True:
        chunk = file.file.read(1024 * 1024)
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="Fichier trop volumineux (max 10 Mo)")
    if not content:
        raise HTTPException(status_code=400, detail="Fichier vide")
    filename = f"{uuid.uuid4()}.{ext}"
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(os.path.join(UPLOAD_DIR, filename), "wb") as f:
        f.write(content)
    return filename

def _save_uploads(files: list[UploadFile], max_count: int = 3) -> list[str]:
    """Validate and save multiple uploaded files, return list of filenames."""
    valid = [f for f in files if f.filename]
    if len(valid) > max_count:
        raise HTTPException(status_code=400, detail=f"Maximum {max_count} fichiers autorisés")
    return [_save_upload(f) for f in valid]


auth_router = APIRouter(prefix="/auth", tags=["auth"])

_FILENAME_RE = re.compile(r"^[a-f0-9\-]+\.\w+$")

REFRESH_COOKIE_NAME = "refresh_token"
REFRESH_COOKIE_PATH = "/auth"


def _is_secure_request(request: Request) -> bool:
    """True when the request came in over HTTPS (direct or via a trusted proxy).

    Browsers refuse Secure cookies on plain HTTP, so we must not set Secure
    when the backend is reached via http://localhost during development.
    """
    if request.url.scheme == "https":
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    return forwarded_proto == "https"


def _set_refresh_cookie(request: Request, response: Response, refresh_token: str) -> None:
    """Set the refresh token as an httpOnly, SameSite=Strict cookie scoped to /auth."""
    settings = get_settings()
    secure = _is_secure_request(request)
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=settings.jwt_refresh_token_expire_days * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(request: Request, response: Response) -> None:
    secure = _is_secure_request(request)
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=secure,
        samesite="strict",
    )


def _split_tokens(tokens: dict) -> tuple[dict, str]:
    """Return (response-safe token dict, refresh_token) separating the refresh token."""
    refresh_token = tokens["refresh_token"]
    response_tokens = {k: v for k, v in tokens.items() if k != "refresh_token"}
    return response_tokens, refresh_token


@auth_router.get("/uploads/{filename}")
def serve_upload(
    filename: str,
    current_user: User = Depends(get_current_user),
):
    """Serve an uploaded file (authenticated)."""
    if not _FILENAME_RE.match(filename) or ".." in filename:
        raise HTTPException(status_code=404)
    path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, headers={"Content-Disposition": f'inline; filename="{filename}"'})


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


@auth_router.post("/signup", response_model=AuthResponse)
@limiter.limit("10/minute")
def signup(request: Request, data: SignUpRequest, response: Response, db: Session = Depends(get_db)):
    try:
        ua = request.headers.get("User-Agent")
        ip = request.client.host if request.client else None
        location = resolve_location(ip)
        user, tokens = register_user(db, data.email, data.password, data.name, device_info=ua, ip_address=ip, location=location)
        response_tokens, refresh_token = _split_tokens(tokens)
        _set_refresh_cookie(request, response, refresh_token)
        log_auth_event(db, event="signup_success", user_id=user.id, request=request, extra={"provider": user.provider})
        return _to_auth_response(user, response_tokens)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@auth_router.post("/login", response_model=AuthResponse)
@limiter.limit("10/minute")
def login(request: Request, data: LoginRequest, response: Response, db: Session = Depends(get_db)):
    try:
        ua = request.headers.get("User-Agent")
        ip = request.client.host if request.client else None
        location = resolve_location(ip)
        user, tokens = authenticate_user(db, data.email, data.password, device_info=ua, ip_address=ip, location=location)
        response_tokens, refresh_token = _split_tokens(tokens)
        _set_refresh_cookie(request, response, refresh_token)
        log_auth_event(db, event="login_success", user_id=user.id, request=request, extra={"provider": user.provider})
        return _to_auth_response(user, response_tokens)
    except ValueError as e:
        log_auth_event(db, event="login_failed", request=request, extra={"email": data.email})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))


@auth_router.post("/google", response_model=AuthResponse)
@limiter.limit("10/minute")
def google_auth(request: Request, data: GoogleAuthRequest, response: Response, db: Session = Depends(get_db)):
    try:
        ua = request.headers.get("User-Agent")
        ip = request.client.host if request.client else None
        location = resolve_location(ip)
        user, tokens = authenticate_google(db, data.credential, device_info=ua, ip_address=ip, location=location)
        response_tokens, refresh_token = _split_tokens(tokens)
        _set_refresh_cookie(request, response, refresh_token)
        return _to_auth_response(user, response_tokens)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))


@auth_router.post("/refresh", response_model=AuthResponse)
@limiter.limit("30/minute")
def refresh(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    refresh_token: str | None = Cookie(default=None),
):
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token manquant")
    try:
        ua = request.headers.get("User-Agent")
        ip = request.client.host if request.client else None
        location = resolve_location(ip)
        user, tokens = refresh_tokens(db, refresh_token, device_info=ua, ip_address=ip, location=location)
        response_tokens, new_refresh_token = _split_tokens(tokens)
        _set_refresh_cookie(request, response, new_refresh_token)
        return _to_auth_response(user, response_tokens)
    except ValueError as e:
        _clear_refresh_cookie(request, response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))


@auth_router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    refresh_token: str | None = Cookie(default=None),
):
    """Revoke the current session (delete UserSession + clear cookie). No auth required."""
    user_id = None
    if refresh_token:
        session = db.query(UserSession).filter(
            UserSession.refresh_token_hash == _hash_token(refresh_token)
        ).first()
        if session:
            user_id = session.user_id
            db.delete(session)
            db.commit()
    _clear_refresh_cookie(request, response)
    log_auth_event(db, event="logout", user_id=user_id, request=request)
    return {"ok": True}


@auth_router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    return _to_user_response(current_user)


@auth_router.post("/forgot-password")
@limiter.limit("10/minute")
def forgot_password(request: Request, data: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Send a password reset email. Always returns 200 to not reveal existing accounts."""
    import uuid
    from datetime import datetime, timedelta, timezone

    from src.auth.email import send_reset_email
    from src.utils.config import get_settings

    user = db.query(User).filter(User.email == data.email).first()
    if user and user.hashed_password:
        # Only allow reset for email-based accounts (not OAuth-only)
        token = str(uuid.uuid4())
        reset_entry = PasswordResetToken(
            user_id=user.id,
            token=token,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(reset_entry)
        db.commit()

        settings = get_settings()
        reset_url = f"{settings.frontend_url}/reset-password?token={token}"
        send_reset_email(user.email, reset_url)

    return {"ok": True}


@auth_router.post("/reset-password")
@limiter.limit("10/minute")
def reset_password(request: Request, data: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Reset password using a valid token."""
    from datetime import datetime, timezone

    from src.auth.security import hash_password

    entry = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token == data.token, PasswordResetToken.used == 0)
        .first()
    )
    if not entry:
        raise HTTPException(status_code=400, detail="Lien invalide ou déjà utilisé")

    # Compare as naive datetimes (SQLite stores naive UTC)
    if entry.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Ce lien a expiré. Veuillez refaire une demande.")

    user = db.query(User).filter(User.id == entry.user_id).first()
    if not user:
        raise HTTPException(status_code=400, detail="Utilisateur non trouvé")

    user.hashed_password = hash_password(data.password)
    user.updated_at = datetime.now(timezone.utc)
    entry.used = 1
    db.commit()

    return {"ok": True, "email": user.email}


@auth_router.post("/verify-email")
@limiter.limit("10/minute")
def verify_email(request: Request, data: VerifyEmailRequest, db: Session = Depends(get_db)):
    """Confirm the email address using a single-use token sent at signup."""
    from datetime import datetime, timezone

    from src.db.models import EmailVerificationToken

    entry = (
        db.query(EmailVerificationToken)
        .filter(EmailVerificationToken.token == data.token, EmailVerificationToken.used == 0)
        .first()
    )
    if not entry:
        raise HTTPException(status_code=400, detail="Lien invalide ou déjà utilisé")
    if entry.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Ce lien a expiré. Demandez un nouvel envoi.")

    user = db.query(User).filter(User.id == entry.user_id).first()
    if not user:
        raise HTTPException(status_code=400, detail="Utilisateur non trouvé")

    user.email_verified = 1
    user.updated_at = datetime.now(timezone.utc)
    entry.used = 1
    db.commit()

    log_auth_event(db, event="email_verified", user_id=user.id, request=request)
    return {"ok": True, "email": user.email}


@auth_router.post("/resend-verification")
@limiter.limit("10/minute")
def resend_verification(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Regenerate a verification token and email it (only if not already verified)."""
    if current_user.email_verified:
        return {"ok": True, "already_verified": True}

    from src.auth.service import _issue_email_verification_token
    from src.auth.email import send_email_verification_email

    token = _issue_email_verification_token(db, current_user)
    verify_url = f"{get_settings().frontend_url}/verify-email?token={token}"
    send_email_verification_email(current_user.email, verify_url)
    return {"ok": True}


@auth_router.post("/request-email-change")
@limiter.limit("5/minute")
def request_email_change(
    request: Request,
    data: RequestEmailChangeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start an email change: emit a token sent to the NEW address for confirmation."""
    from datetime import datetime, timedelta, timezone

    from src.auth.email import send_email_change_email
    from src.db.models import EmailChangeToken

    new_email = data.new_email.strip().lower()
    if not new_email or "@" not in new_email:
        raise HTTPException(status_code=400, detail="Email invalide")
    if new_email == current_user.email.lower():
        raise HTTPException(status_code=400, detail="Cet email est déjà le vôtre")

    # Unicity - ignore trial ghost accounts (they are replaceable)
    taken = (
        db.query(User)
        .filter(User.email == new_email, User.id != current_user.id)
        .first()
    )
    if taken and taken.provider != "trial":
        raise HTTPException(status_code=400, detail="Cet email est déjà utilisé")

    # Drop any pending tokens so the user can't have multiple confirm links floating
    db.query(EmailChangeToken).filter(
        EmailChangeToken.user_id == current_user.id,
        EmailChangeToken.used == 0,
    ).delete()

    token = str(uuid.uuid4())
    db.add(EmailChangeToken(
        user_id=current_user.id,
        new_email=new_email,
        token=token,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        used=0,
    ))
    db.commit()

    verify_url = f"{get_settings().frontend_url}/verify-email-change?token={token}"
    send_email_change_email(new_email, verify_url)
    log_auth_event(db, event="email_change_requested", user_id=current_user.id, request=request, extra={"new_email": new_email})
    return {"ok": True}


@auth_router.post("/verify-email-change")
@limiter.limit("10/minute")
def verify_email_change(request: Request, data: VerifyEmailChangeRequest, db: Session = Depends(get_db)):
    """Finalize an email change using the token sent to the new address."""
    from datetime import datetime, timezone

    from src.db.models import EmailChangeToken

    entry = (
        db.query(EmailChangeToken)
        .filter(EmailChangeToken.token == data.token, EmailChangeToken.used == 0)
        .first()
    )
    if not entry:
        raise HTTPException(status_code=400, detail="Lien invalide ou déjà utilisé")
    if entry.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Ce lien a expiré.")

    user = db.query(User).filter(User.id == entry.user_id).first()
    if not user:
        raise HTTPException(status_code=400, detail="Utilisateur non trouvé")

    # Race-check unicity: another account may have grabbed the email in the meantime
    taken = (
        db.query(User)
        .filter(User.email == entry.new_email, User.id != user.id)
        .first()
    )
    if taken and taken.provider != "trial":
        raise HTTPException(status_code=400, detail="Cet email a été pris entre-temps")

    user.email = entry.new_email
    user.email_verified = 1
    user.updated_at = datetime.now(timezone.utc)
    entry.used = 1
    db.commit()
    log_auth_event(db, event="email_changed", user_id=user.id, request=request, extra={"new_email": entry.new_email})
    return {"ok": True, "email": user.email}


@auth_router.post("/history", response_model=SearchHistoryEntry)
def add_history(
    data: AddHistoryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    # Upsert: update timestamp if same article already exists, otherwise create
    existing = (
        db.query(SearchHistory)
        .filter(
            SearchHistory.user_id == current_user.id,
            SearchHistory.article_ref == data.article_ref,
        )
        .first()
    )

    if existing:
        existing.searched_at = now
        existing.article_title = data.article_title
        db.commit()
        db.refresh(existing)
        entry = existing
    else:
        entry = SearchHistory(
            user_id=current_user.id,
            article_ref=data.article_ref,
            article_title=data.article_title,
            searched_at=now,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
    return SearchHistoryEntry(
        article_ref=entry.article_ref,
        article_title=entry.article_title,
        searched_at=entry.searched_at.isoformat(),
    )


@auth_router.post("/report-article")
def report_article(
    data: ReportArticleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Report a missing article."""
    from datetime import datetime, timezone

    # Prevent duplicate report from same user for same article
    existing = (
        db.query(ArticleReport)
        .filter(
            ArticleReport.user_id == current_user.id,
            ArticleReport.article_ref == data.article_ref,
        )
        .first()
    )
    if existing:
        # Update notify preference if changed
        if data.notify and existing.notify == 0:
            existing.notify = 1
            db.commit()
        return {"ok": True, "already_reported": True}

    entry = ArticleReport(
        user_id=current_user.id,
        article_ref=data.article_ref,
        notify=1 if data.notify else 0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(entry)
    db.commit()
    return {"ok": True}


@auth_router.post("/contact")
def contact(
    data: ContactRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Submit a contact/support message."""
    from datetime import datetime, timezone

    entry = ContactMessage(
        user_id=current_user.id,
        email=data.email,
        message=data.message,
        created_at=datetime.now(timezone.utc),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)

    # Notify admins by email
    from src.auth.email import send_contact_notification_email
    from src.utils.config import get_settings as _gs
    for admin_email in _gs().get_admin_emails():
        send_contact_notification_email(admin_email, data.email, data.message)

    return {"ok": True, "ticket_id": entry.id}


@auth_router.get("/conversations")
def get_conversations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get all contact tickets for the current user."""
    tickets = (
        db.query(ContactMessage)
        .filter(ContactMessage.user_id == current_user.id)
        .order_by(ContactMessage.created_at.desc())
        .all()
    )
    result = []
    for t in tickets:
        last_msg = (
            db.query(ConversationMessage)
            .filter(ConversationMessage.contact_id == t.id)
            .order_by(ConversationMessage.created_at.desc())
            .first()
        )
        result.append({
            "id": t.id,
            "message": t.message,
            "status": t.status,
            "created_at": t.created_at.isoformat(),
            "last_message": last_msg.message[:80] if last_msg else None,
            "last_sender": last_msg.sender if last_msg else None,
            "last_message_at": last_msg.created_at.isoformat() if last_msg else None,
        })
    return result


@auth_router.get("/conversations/{contact_id}")
def get_conversation(
    contact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a full conversation thread."""
    ticket = (
        db.query(ContactMessage)
        .filter(ContactMessage.id == contact_id, ContactMessage.user_id == current_user.id)
        .first()
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket non trouvé")

    # Track last seen for delayed email logic
    from datetime import datetime, timezone
    ticket.last_seen_at = datetime.now(timezone.utc)
    db.commit()

    messages = (
        db.query(ConversationMessage)
        .filter(ConversationMessage.contact_id == contact_id)
        .order_by(ConversationMessage.created_at.asc())
        .all()
    )
    return {
        "id": ticket.id,
        "email": ticket.email,
        "message": ticket.message,
        "status": ticket.status,
        "created_at": ticket.created_at.isoformat(),
        "messages": [
            {
                "id": m.id,
                "sender": m.sender,
                "message": m.message,
                "file_path": m.file_path,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ],
    }


@auth_router.post("/conversations/{contact_id}/close")
def close_conversation(
    contact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Close a ticket."""
    ticket = (
        db.query(ContactMessage)
        .filter(ContactMessage.id == contact_id, ContactMessage.user_id == current_user.id)
        .first()
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket non trouvé")
    ticket.status = "closed"
    db.commit()
    return {"ok": True}


@auth_router.post("/conversations/{contact_id}/messages")
def add_conversation_message(
    contact_id: int,
    message: str = Form(""),
    files: list[UploadFile] = File([]),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add a message to an existing ticket."""
    import json as _json
    from datetime import datetime, timezone

    saved = _save_uploads(files) if files else []
    if not message.strip() and not saved:
        raise HTTPException(status_code=400, detail="Message ou fichier requis")

    ticket = (
        db.query(ContactMessage)
        .filter(ContactMessage.id == contact_id, ContactMessage.user_id == current_user.id)
        .first()
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket non trouvé")
    if ticket.status == "closed":
        raise HTTPException(status_code=400, detail="Ce ticket est fermé")

    msg = ConversationMessage(
        contact_id=contact_id,
        sender="user",
        message=message.strip(),
        file_path=_json.dumps(saved) if saved else None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    db.commit()

    # Notify admins
    from src.auth.email import send_contact_notification_email
    from src.utils.config import get_settings as _gs
    for admin_email in _gs().get_admin_emails():
        send_contact_notification_email(admin_email, ticket.email, message.strip() or "(fichier joint)")

    return {"ok": True}


@auth_router.post("/feedback")
def feedback(
    data: FeedbackRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Submit feedback on an article analysis. Upserts by user + article + section."""
    from datetime import datetime, timezone

    if data.section_index is None:
        existing = (
            db.query(AnalysisFeedback)
            .filter(
                AnalysisFeedback.user_id == current_user.id,
                AnalysisFeedback.article_ref == data.article_ref,
                AnalysisFeedback.section_index.is_(None),
            )
            .order_by(AnalysisFeedback.created_at.desc())
            .first()
        )
    else:
        existing = (
            db.query(AnalysisFeedback)
            .filter(
                AnalysisFeedback.user_id == current_user.id,
                AnalysisFeedback.article_ref == data.article_ref,
                AnalysisFeedback.section_index == data.section_index,
            )
            .order_by(AnalysisFeedback.created_at.desc())
            .first()
        )

    if data.rating is None:
        # Clear vote
        if existing and existing.message:
            # Has comment → keep old entry intact, create new with null rating
            db.add(AnalysisFeedback(
                user_id=current_user.id,
                article_ref=data.article_ref,
                section_index=data.section_index,
                rating=None,
                message=None,
                created_at=datetime.now(timezone.utc),
            ))
        elif existing:
            # No comment → delete the row entirely
            db.delete(existing)
    elif existing and existing.message:
        # Has comment → keep old entry intact, create new with new rating
        db.add(AnalysisFeedback(
            user_id=current_user.id,
            article_ref=data.article_ref,
            section_index=data.section_index,
            rating=data.rating,
            message=data.message,
            created_at=datetime.now(timezone.utc),
        ))
    elif existing:
        existing.rating = data.rating
        existing.message = data.message
        existing.created_at = datetime.now(timezone.utc)
    else:
        db.add(AnalysisFeedback(
            user_id=current_user.id,
            article_ref=data.article_ref,
            section_index=data.section_index,
            rating=data.rating,
            message=data.message,
            created_at=datetime.now(timezone.utc),
        ))
    db.commit()
    return {"ok": True}


@auth_router.get("/feedback")
def get_feedback(
    article_ref: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get user's existing feedback for an article."""
    entries = (
        db.query(AnalysisFeedback)
        .filter(
            AnalysisFeedback.user_id == current_user.id,
            AnalysisFeedback.article_ref == article_ref,
        )
        .order_by(AnalysisFeedback.created_at.desc())
        .all()
    )
    # For each section, return the latest rating + the full history of the
    # user's own comments (most recent first). Each comment carries the rating
    # active at the time of submission and any admin reply targeting it.
    sections: dict[int | None, dict] = {}
    for e in entries:  # entries are sorted desc by created_at
        bucket = sections.get(e.section_index)
        if bucket is None:
            bucket = {
                "section_index": e.section_index,
                "rating": e.rating,  # latest entry's rating
                "comments": [],
            }
            sections[e.section_index] = bucket
        if e.message:
            bucket["comments"].append({
                "id": e.id,
                "rating": e.rating,
                "message": e.message,
                "admin_reply": e.admin_reply,
                "admin_reply_at": e.admin_reply_at.isoformat() if e.admin_reply_at else None,
                "created_at": e.created_at.isoformat(),
            })

    return list(sections.values())


@auth_router.get("/notifications")
def get_notifications(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get notifications for the current user."""
    entries = (
        db.query(Notification)
        .filter(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(20)
        .all()
    )
    return [
        {
            "id": n.id,
            "title": n.title,
            "message": n.message,
            "link": n.link,
            "type": n.type,
            "metadata": n.extra_data,
            "read": n.read,
            "created_at": n.created_at.isoformat(),
        }
        for n in entries
    ]


@auth_router.post("/notifications/read-all")
def mark_all_notifications_read(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark all notifications as read for the current user."""
    db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.read == 0,
    ).update({"read": 1})
    db.commit()
    return {"ok": True}


@auth_router.post("/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark a notification as read."""
    entry = (
        db.query(Notification)
        .filter(Notification.id == notification_id, Notification.user_id == current_user.id)
        .first()
    )
    if entry:
        entry.read = 1
        db.commit()
    return {"ok": True}


@auth_router.delete("/notifications/{notification_id}")
def delete_notification(
    notification_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a notification."""
    db.query(Notification).filter(
        Notification.id == notification_id,
        Notification.user_id == current_user.id,
    ).delete()
    db.commit()
    return {"ok": True}


@auth_router.get("/history", response_model=SearchHistoryResponse)
def get_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    entries = (
        db.query(SearchHistory)
        .filter(SearchHistory.user_id == current_user.id)
        .order_by(SearchHistory.searched_at.desc())
        .limit(50)
        .all()
    )
    return SearchHistoryResponse(
        entries=[
            SearchHistoryEntry(
                article_ref=e.article_ref,
                article_title=e.article_title,
                searched_at=e.searched_at.isoformat(),
            )
            for e in entries
        ]
    )


@auth_router.post("/change-password")
def change_password(
    data: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change password for email-based accounts."""
    from datetime import datetime, timezone

    from src.auth.security import hash_password, verify_password

    if not current_user.hashed_password:
        raise HTTPException(status_code=400, detail="Changement de mot de passe non disponible pour les comptes Google")

    if not verify_password(data.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Mot de passe actuel incorrect")

    current_user.hashed_password = hash_password(data.new_password)
    current_user.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_auth_event(db, event="password_changed", user_id=current_user.id)
    return {"ok": True}


@auth_router.delete("/account")
def delete_account(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete the current user's account and all associated data."""
    from datetime import datetime, timezone

    provider = current_user.provider
    db.query(UserSession).filter(UserSession.user_id == current_user.id).delete()
    db.query(PasswordResetToken).filter(PasswordResetToken.user_id == current_user.id).delete()
    db.query(SearchHistory).filter(SearchHistory.user_id == current_user.id).delete()
    db.query(Subscription).filter(Subscription.user_id == current_user.id).delete()
    db.query(EmailVerificationToken).filter(EmailVerificationToken.user_id == current_user.id).delete()
    db.query(EmailChangeToken).filter(EmailChangeToken.user_id == current_user.id).delete()
    db.query(Notification).filter(Notification.user_id == current_user.id).delete()
    db.query(AnalysisFeedback).filter(AnalysisFeedback.user_id == current_user.id).delete()
    db.query(ArticleReport).filter(ArticleReport.user_id == current_user.id).update({"user_id": None})
    db.query(ContactMessage).filter(ContactMessage.user_id == current_user.id).update({"user_id": None})
    db.query(TrialLink).filter(TrialLink.user_id == current_user.id).update({"user_id": None})
    db.query(TrialLink).filter(TrialLink.created_by == current_user.id).delete()
    db.query(User).filter(User.id == current_user.id).delete()
    db.add(AccountLog(event="account_deleted", provider=provider, timestamp=datetime.now(timezone.utc)))
    db.commit()
    _clear_refresh_cookie(request, response)
    # Note: user row is already gone, so no FK on auth_events; we log with null user_id.
    log_auth_event(db, event="account_deleted", request=request, extra={"provider": provider})
    return {"ok": True}


@auth_router.delete("/history")
def clear_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.query(SearchHistory).filter(SearchHistory.user_id == current_user.id).delete()
    db.commit()
    return {"ok": True}


@auth_router.get("/sessions")
def get_sessions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    refresh_token: str | None = Cookie(default=None),
):
    """Get active sessions for the current user. Marks the cookie-bearing one as current."""
    sessions = (
        db.query(UserSession)
        .filter(UserSession.user_id == current_user.id)
        .order_by(UserSession.last_used_at.desc())
        .all()
    )

    current_hash = _hash_token(refresh_token) if refresh_token else None

    return [
        {
            "id": s.id,
            "device_info": s.device_info,
            "location": s.location,
            "created_at": s.created_at.isoformat(),
            "last_used_at": s.last_used_at.isoformat(),
            "is_current": s.refresh_token_hash == current_hash if current_hash else False,
        }
        for s in sessions
    ]


@auth_router.delete("/sessions/{session_id}")
def revoke_session(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke a specific session."""
    deleted = db.query(UserSession).filter(
        UserSession.id == session_id,
        UserSession.user_id == current_user.id,
    ).delete()
    db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="Session non trouvée")
    return {"ok": True}


@auth_router.delete("/sessions")
def revoke_all_sessions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    refresh_token: str | None = Cookie(default=None),
):
    """Revoke all sessions except the current one (identified by cookie).

    Requires the refresh_token cookie so we can identify and preserve the
    current session. Without it, we'd delete the caller's own session and
    silently log them out at the next access-token expiration.
    """
    if not refresh_token:
        raise HTTPException(status_code=400, detail="missing_refresh_cookie")
    current_hash = _hash_token(refresh_token)
    db.query(UserSession).filter(
        UserSession.user_id == current_user.id,
        UserSession.refresh_token_hash != current_hash,
    ).delete()
    db.commit()
    return {"ok": True}
