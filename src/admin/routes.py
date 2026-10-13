"""Admin API routes."""

import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.auth.dependencies import get_admin_user, get_db
from src.auth.email import send_article_added_email, send_contact_reply_email, send_feedback_reply_email
from src.auth.schemas import CreateTrialLinkBatchRequest, CreateTrialLinkRequest, TrialLinkResponse
from src.db.models import (
    AnalysisFeedback,
    ArticleReport,
    ContactMessage,
    ConversationMessage,
    Notification,
    SearchHistory,
    TrialLink,
    TrialLinkVisit,
    User,
)


class ReplyRequest(BaseModel):
    message: str

logger = logging.getLogger(__name__)

admin_router = APIRouter(prefix="/admin", tags=["admin"])


@admin_router.get("/uploads/{filename}")
def serve_upload_admin(
    filename: str,
    admin: User = Depends(get_admin_user),
):
    """Serve an uploaded file (admin only)."""
    from src.auth.routes import UPLOAD_DIR, _FILENAME_RE
    if not _FILENAME_RE.match(filename) or ".." in filename:
        raise HTTPException(status_code=404)
    path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, headers={"Content-Disposition": f'inline; filename="{filename}"'})


@admin_router.get("/stats")
def get_stats(
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    total_users = db.query(func.count(User.id)).scalar()
    total_reports = db.query(func.count(ArticleReport.id)).scalar()
    total_feedbacks = db.query(func.count(AnalysisFeedback.id)).scalar()
    feedbacks_up = db.query(func.count(AnalysisFeedback.id)).filter(AnalysisFeedback.rating == "up").scalar()
    feedbacks_down = db.query(func.count(AnalysisFeedback.id)).filter(AnalysisFeedback.rating == "down").scalar()
    total_contacts = db.query(func.count(ContactMessage.id)).scalar()

    # Trial conversion stats: converted / redeemed (i.e. opened at least once)
    trials_redeemed = db.query(func.count(TrialLink.id)).filter(
        TrialLink.redeemed_at.isnot(None)
    ).scalar() or 0
    trials_converted = db.query(func.count(TrialLink.id)).filter(
        TrialLink.converted_at.isnot(None)
    ).scalar() or 0

    return {
        "users": total_users,
        "reports": total_reports,
        "feedbacks": total_feedbacks,
        "feedbacks_up": feedbacks_up,
        "feedbacks_down": feedbacks_down,
        "contacts": total_contacts,
        "trials_redeemed": trials_redeemed,
        "trials_converted": trials_converted,
    }


@admin_router.get("/reports")
def get_reports(
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List reports grouped by article_ref."""
    reports = (
        db.query(ArticleReport, User)
        .outerjoin(User, ArticleReport.user_id == User.id)
        .order_by(ArticleReport.created_at.desc())
        .all()
    )
    # Group by article_ref
    grouped: dict[str, dict] = {}
    for r, u in reports:
        ref = r.article_ref
        if ref not in grouped:
            grouped[ref] = {
                "article_ref": ref,
                "count": 0,
                "notify_count": 0,
                "users": [],
                "first_created_at": r.created_at.isoformat(),
            }
        grouped[ref]["count"] += 1
        if r.notify == 1:
            grouped[ref]["notify_count"] += 1
        if u:
            grouped[ref]["users"].append({
                "name": u.name,
                "email": u.email,
            })
    return list(grouped.values())


class ResolveByRefRequest(BaseModel):
    article_ref: str


@admin_router.post("/reports/resolve")
def resolve_reports_by_ref(
    data: ResolveByRefRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Resolve all reports for an article: notify users who requested it, then delete."""
    article_ref = data.article_ref

    reports = db.query(ArticleReport).filter(ArticleReport.article_ref == article_ref).all()
    if not reports:
        raise HTTPException(status_code=404, detail="Signalement non trouvé")

    # Find all reports for this article with notify=1
    notify_reports = (
        db.query(ArticleReport, User)
        .outerjoin(User, ArticleReport.user_id == User.id)
        .filter(ArticleReport.article_ref == article_ref, ArticleReport.notify == 1)
        .all()
    )

    notified_users: set[str] = set()
    for r, u in notify_reports:
        if u and u.id not in notified_users:
            notified_users.add(u.id)
            # Send email
            send_article_added_email(u.email, article_ref)
            # Create in-app notification
            db.add(Notification(
                user_id=u.id,
                title="Article ajouté",
                message=f"L'article {article_ref} que vous avez signalé a été ajouté.",
                link=f"/results/article/{article_ref}",
                type="article_added",
                created_at=datetime.now(timezone.utc),
            ))

    # Delete all reports for this article
    db.query(ArticleReport).filter(ArticleReport.article_ref == article_ref).delete()
    db.commit()

    return {"ok": True, "notified": len(notified_users)}


@admin_router.get("/contacts")
def get_contacts(
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    contacts = (
        db.query(ContactMessage)
        .order_by(ContactMessage.created_at.desc())
        .limit(100)
        .all()
    )
    result = []
    for c in contacts:
        msg_count = db.query(func.count(ConversationMessage.id)).filter(
            ConversationMessage.contact_id == c.id
        ).scalar()
        result.append({
            "id": c.id,
            "email": c.email,
            "message": c.message,
            "status": c.status,
            "message_count": msg_count,
            "created_at": c.created_at.isoformat(),
        })
    return result


@admin_router.get("/feedbacks")
def get_feedbacks(
    article_ref: str | None = None,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    query = (
        db.query(AnalysisFeedback, User.name)
        .outerjoin(User, AnalysisFeedback.user_id == User.id)
        .filter(AnalysisFeedback.rating.isnot(None))
        .order_by(AnalysisFeedback.created_at.desc())
    )
    if article_ref:
        query = query.filter(AnalysisFeedback.article_ref == article_ref)
    else:
        query = query.limit(100)
    return [
        {
            "id": f.id,
            "article_ref": f.article_ref,
            "section_index": f.section_index,
            "rating": f.rating,
            "message": f.message,
            "user_name": user_name or "Utilisateur",
            "created_at": f.created_at.isoformat(),
            "admin_reply": f.admin_reply,
            "admin_reply_at": f.admin_reply_at.isoformat() if f.admin_reply_at else None,
        }
        for f, user_name in query.all()
    ]


@admin_router.post("/feedbacks/{feedback_id}/reply")
def reply_feedback(
    feedback_id: int,
    data: ReplyRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Reply to a user feedback/comment on an article.

    Stores the reply on the feedback row, creates an in-app notification, and
    sends an email via Resend so the commenter knows to come back.
    """
    reply_text = (data.message or "").strip()
    if not reply_text:
        raise HTTPException(status_code=400, detail="Message requis")

    feedback = db.query(AnalysisFeedback).filter(AnalysisFeedback.id == feedback_id).first()
    if not feedback:
        raise HTTPException(status_code=404, detail="Retour non trouvé")

    user = db.query(User).filter(User.id == feedback.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Auteur du retour introuvable")

    now = datetime.now(timezone.utc)
    feedback.admin_reply = reply_text
    feedback.admin_reply_at = now
    feedback.admin_reply_by = admin.id

    link = f"/results/article/{feedback.article_ref}#feedback-{feedback.id}"

    db.add(Notification(
        user_id=user.id,
        title="Réponse à votre retour",
        message=reply_text,
        link=link,
        type="feedback_reply",
        created_at=now,
    ))

    db.commit()

    send_feedback_reply_email(
        user.email,
        feedback.article_ref,
        feedback.message or "",
        reply_text,
        feedback.section_index,
    )

    return {
        "ok": True,
        "admin_reply": reply_text,
        "admin_reply_at": now.isoformat(),
    }


def _send_delayed_contact_email(
    contact_id: int, to_email: str, original_msg: str, reply_msg: str, replied_at_iso: str,
):
    """Background task: wait 2 min, then send email only if user hasn't seen the message."""
    import time
    time.sleep(120)

    from src.db.models import get_session
    db = get_session()
    try:
        contact = db.query(ContactMessage).filter(ContactMessage.id == contact_id).first()
        replied_at = datetime.fromisoformat(replied_at_iso)
        # Send email only if user hasn't viewed the conversation since the reply
        if contact and (not contact.last_seen_at or contact.last_seen_at < replied_at):
            send_contact_reply_email(to_email, original_msg, reply_msg, contact_id)
    finally:
        db.close()


@admin_router.post("/contacts/{contact_id}/reply")
def reply_contact(
    contact_id: int,
    background_tasks: BackgroundTasks,
    message: str = Form(""),
    files: list[UploadFile] = File([]),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Reply to a contact message - adds to thread, sends delayed notification email."""
    import json as _json
    from src.auth.routes import _save_uploads

    saved = _save_uploads(files) if files else []
    if not message.strip() and not saved:
        raise HTTPException(status_code=400, detail="Message ou fichier requis")

    contact = db.query(ContactMessage).filter(ContactMessage.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Message non trouvé")
    if contact.status == "closed":
        raise HTTPException(status_code=400, detail="Ce ticket est fermé")

    replied_at = datetime.now(timezone.utc)

    # Add message to conversation thread
    db.add(ConversationMessage(
        contact_id=contact_id,
        sender="admin",
        message=message.strip(),
        file_path=_json.dumps(saved) if saved else None,
        created_at=replied_at,
    ))

    # Create in-app notification with link to ticket
    if contact.user_id:
        db.add(Notification(
            user_id=contact.user_id,
            title="Réponse à votre message",
            message=message.strip() or "(fichier joint)",
            type="contact_reply",
            link=f"/contact?ticket={contact_id}",
            created_at=replied_at,
        ))

    db.commit()

    # Schedule delayed email (sends only if user hasn't seen the message after 2 min)
    background_tasks.add_task(
        _send_delayed_contact_email,
        contact_id, contact.email, contact.message, message.strip() or "(fichier joint)", replied_at.isoformat(),
    )

    return {"ok": True}


@admin_router.post("/contacts/{contact_id}/close")
def close_contact(
    contact_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Close a contact ticket."""
    contact = db.query(ContactMessage).filter(ContactMessage.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Ticket non trouvé")
    contact.status = "closed"
    db.commit()
    return {"ok": True}


@admin_router.get("/contacts/{contact_id}/thread")
def get_contact_thread(
    contact_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Get full conversation thread for a contact ticket."""
    contact = db.query(ContactMessage).filter(ContactMessage.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Ticket non trouvé")

    messages = (
        db.query(ConversationMessage)
        .filter(ConversationMessage.contact_id == contact_id)
        .order_by(ConversationMessage.created_at.asc())
        .all()
    )
    return {
        "id": contact.id,
        "email": contact.email,
        "message": contact.message,
        "status": contact.status,
        "created_at": contact.created_at.isoformat(),
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


@admin_router.get("/users")
def get_users(
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List all users (read-only)."""
    users = (
        db.query(User)
        .order_by(User.created_at.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id": u.id,
            "email": u.email,
            "name": u.name,
            "provider": u.provider,
            "role": u.role,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


@admin_router.get("/stats/timeline")
def get_stats_timeline(
    days: int = 30,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Return daily counts for users, feedbacks, reports, analyses over a given period."""
    if days not in (1, 7, 30, 180):
        days = 30
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    # Query each metric grouped by date
    user_counts = {
        str(k): v
        for k, v in db.query(func.date(User.created_at), func.count(User.id))
        .filter(User.created_at >= since)
        .group_by(func.date(User.created_at))
        .all()
    }
    feedback_counts = {
        str(k): v
        for k, v in db.query(func.date(AnalysisFeedback.created_at), func.count(AnalysisFeedback.id))
        .filter(AnalysisFeedback.created_at >= since, AnalysisFeedback.rating.isnot(None))
        .group_by(func.date(AnalysisFeedback.created_at))
        .all()
    }
    report_counts = {
        str(k): v
        for k, v in db.query(func.date(ArticleReport.created_at), func.count(ArticleReport.id))
        .filter(ArticleReport.created_at >= since)
        .group_by(func.date(ArticleReport.created_at))
        .all()
    }
    analysis_counts = {
        str(k): v
        for k, v in db.query(func.date(SearchHistory.searched_at), func.count(SearchHistory.id))
        .filter(SearchHistory.searched_at >= since)
        .group_by(func.date(SearchHistory.searched_at))
        .all()
    }

    # Build day array
    result_days = []
    for i in range(days):
        day = (now - timedelta(days=days - 1 - i)).date()
        day_str = str(day)
        result_days.append({
            "date": day_str,
            "users": user_counts.get(day_str, 0),
            "feedbacks": feedback_counts.get(day_str, 0),
            "reports": report_counts.get(day_str, 0),
            "analyses": analysis_counts.get(day_str, 0),
        })

    return {"days": result_days}


@admin_router.get("/searches/recent")
def get_recent_searches(
    limit: int = 50,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Last N searches across all users."""
    limit = max(1, min(limit, 200))
    rows = (
        db.query(SearchHistory, User)
        .join(User, SearchHistory.user_id == User.id)
        .order_by(SearchHistory.searched_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "article_ref": h.article_ref,
            "article_title": h.article_title,
            "searched_at": h.searched_at.isoformat(),
            "user_id": u.id,
            "user_email": u.email,
            "user_name": u.name,
        }
        for h, u in rows
    ]


@admin_router.get("/searches/top")
def get_top_searches(
    days: int = 30,
    limit: int = 20,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Top searched article refs over the given period."""
    if days not in (1, 7, 30, 180):
        days = 30
    limit = max(1, min(limit, 100))
    since = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (
        db.query(
            SearchHistory.article_ref,
            func.max(SearchHistory.article_title).label("title"),
            func.count(SearchHistory.id).label("count"),
            func.count(func.distinct(SearchHistory.user_id)).label("unique_users"),
        )
        .filter(SearchHistory.searched_at >= since)
        .group_by(SearchHistory.article_ref)
        .order_by(func.count(SearchHistory.id).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "article_ref": ref,
            "article_title": title,
            "count": count,
            "unique_users": unique_users,
        }
        for ref, title, count, unique_users in rows
    ]


# --- Trial links management ---


def _trial_link_to_response(
    trial_link: TrialLink,
    db: Session,
    *,
    search_count: int | None = None,
    last_activity: str | None = None,
) -> dict:
    """Convert a TrialLink to response dict with metrics.

    When `search_count` / `last_activity` are passed (e.g. precomputed by a
    batch aggregation in the list endpoint), no per-link query is issued. This
    avoids the N+1 pattern when serializing many links at once.
    """
    if search_count is None and trial_link.user_id:
        search_count = db.query(func.count(SearchHistory.id)).filter(
            SearchHistory.user_id == trial_link.user_id
        ).scalar() or 0
        last_search = db.query(func.max(SearchHistory.searched_at)).filter(
            SearchHistory.user_id == trial_link.user_id
        ).scalar()
        if last_search:
            last_activity = last_search.isoformat()
    if search_count is None:
        search_count = 0

    from src.utils.config import get_settings
    settings = get_settings()
    frontend_url = getattr(settings, "frontend_url", "")

    return {
        "id": trial_link.id,
        "token": trial_link.token,
        "prospect_email": trial_link.prospect_email,
        "prospect_name": trial_link.prospect_name,
        "status": trial_link.status,
        "created_at": trial_link.created_at.isoformat(),
        "expires_at": trial_link.expires_at.isoformat() if trial_link.expires_at else None,
        "link_expires_at": trial_link.link_expires_at.isoformat(),
        "redeemed_at": trial_link.redeemed_at.isoformat() if trial_link.redeemed_at else None,
        "converted_at": trial_link.converted_at.isoformat() if trial_link.converted_at else None,
        "visit_count": trial_link.visit_count or 0,
        "search_count": search_count,
        "last_activity": last_activity,
        "link_url": f"{frontend_url}/trial?token={trial_link.token}" if frontend_url else f"/trial?token={trial_link.token}",
    }


def _create_single_trial_link(
    db: Session, prospect_email: str, prospect_name: str | None, admin_id: str
) -> dict | None:
    """Create a single trial link. Returns response dict or None if email already has a non-trial account."""
    email = prospect_email.strip().lower()

    # Check for existing active/pending trial link (idempotent)
    existing_trial = db.query(TrialLink).filter(
        TrialLink.prospect_email == email,
        TrialLink.status.in_(["pending", "active"]),
    ).first()
    if existing_trial:
        return _trial_link_to_response(existing_trial, db)

    # Check for existing non-trial user
    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user and existing_user.provider != "trial":
        return None  # Cannot create trial for existing user

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Trial links no longer expire before first open - sentinel far-future
    # value satisfies the NOT NULL column without imposing a real deadline.
    far_future = datetime(9999, 12, 31)
    trial_link = TrialLink(
        token=secrets.token_hex(32),
        prospect_email=email,
        prospect_name=prospect_name,
        status="pending",
        created_at=now,
        expires_at=None,  # Set on first redemption
        link_expires_at=far_future,
        created_by=admin_id,
    )
    db.add(trial_link)
    db.commit()
    db.refresh(trial_link)

    return _trial_link_to_response(trial_link, db)


@admin_router.post("/trial-links")
def create_trial_link(
    data: CreateTrialLinkRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    result = _create_single_trial_link(db, data.prospect_email, data.prospect_name, admin.id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Un compte existe déjà avec cet email.",
        )
    return result


@admin_router.post("/trial-links/batch")
def create_trial_links_batch(
    data: CreateTrialLinkBatchRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    results = []
    for email in data.emails:
        try:
            result = _create_single_trial_link(db, email, None, admin.id)
            if result is None:
                results.append({"email": email.strip().lower(), "status": "error", "detail": "Compte existant"})
            else:
                results.append({"email": email.strip().lower(), "status": "ok", "trial_link": result})
        except Exception as e:
            results.append({"email": email.strip().lower(), "status": "error", "detail": str(e)})
    return results


@admin_router.get("/trial-links")
def list_trial_links(
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    # Aggregate search metrics per user in a single grouped query, then join
    # to trial_links — avoids the 2-queries-per-link N+1 we used to do when
    # serializing the list.
    search_agg = (
        db.query(
            SearchHistory.user_id.label("user_id"),
            func.count(SearchHistory.id).label("search_count"),
            func.max(SearchHistory.searched_at).label("last_search"),
        )
        .group_by(SearchHistory.user_id)
        .subquery()
    )

    rows = (
        db.query(
            TrialLink,
            search_agg.c.search_count,
            search_agg.c.last_search,
        )
        .outerjoin(search_agg, search_agg.c.user_id == TrialLink.user_id)
        .order_by(TrialLink.created_at.desc())
        .all()
    )

    # Auto-expire active trials past their trial deadline. Pending links no
    # longer expire (we removed the pre-redemption deadline).
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expired_any = False
    for link, _, _ in rows:
        if link.status == "active" and link.expires_at and link.expires_at < now:
            link.status = "expired"
            expired_any = True
    if expired_any:
        db.commit()

    return [
        _trial_link_to_response(
            link,
            db,
            search_count=int(search_count or 0),
            last_activity=last_search.isoformat() if last_search else None,
        )
        for link, search_count, last_search in rows
    ]


@admin_router.post("/trial-links/{link_id}/revoke")
def revoke_trial_link(
    link_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    link = db.query(TrialLink).filter(TrialLink.id == link_id).first()
    if not link:
        raise HTTPException(status_code=404, detail="Lien non trouvé")
    link.status = "revoked"
    db.commit()
    return {"ok": True}


@admin_router.post("/trial-links/{link_id}/extend")
def extend_trial_link(
    link_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    link = db.query(TrialLink).filter(TrialLink.id == link_id).first()
    if not link:
        raise HTTPException(status_code=404, detail="Lien non trouvé")

    from src.utils.config import get_settings
    settings = get_settings()
    duration = getattr(settings, "trial_duration_days", 7)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if link.expires_at:
        # Trial already activated - extend from current expiration (or from
        # now if it had already lapsed) so admins recover a usable window.
        base = link.expires_at if link.expires_at > now else now
        link.expires_at = base + timedelta(days=duration)

    if link.status in ("expired", "revoked"):
        link.status = "active" if link.user_id else "pending"
    db.commit()
    return _trial_link_to_response(link, db)


@admin_router.get("/trial-links/{link_id}/visits")
def list_trial_link_visits(
    link_id: int,
    limit: int = 100,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List visits (inspect + redeem hits) for a trial link, most recent first.

    Each visit is classified as human / bot / unknown to help admins see when
    email-security scanners pre-fetch the link vs when the prospect actually
    visits.
    """
    link = db.query(TrialLink).filter(TrialLink.id == link_id).first()
    if not link:
        raise HTTPException(status_code=404, detail="Lien non trouvé")

    limit = max(1, min(limit, 500))
    visits = (
        db.query(TrialLinkVisit)
        .filter(TrialLinkVisit.trial_link_id == link_id)
        .order_by(TrialLinkVisit.created_at.desc())
        .limit(limit)
        .all()
    )

    total = db.query(func.count(TrialLinkVisit.id)).filter(TrialLinkVisit.trial_link_id == link_id).scalar() or 0
    human_count = db.query(func.count(TrialLinkVisit.id)).filter(
        TrialLinkVisit.trial_link_id == link_id,
        TrialLinkVisit.classification == "human",
    ).scalar() or 0
    bot_count = db.query(func.count(TrialLinkVisit.id)).filter(
        TrialLinkVisit.trial_link_id == link_id,
        TrialLinkVisit.classification == "bot",
    ).scalar() or 0

    return {
        "trial_link_id": link_id,
        "total": total,
        "human_count": human_count,
        "bot_count": bot_count,
        "unknown_count": total - human_count - bot_count,
        "visits": [
            {
                "id": v.id,
                "event_type": v.event_type,
                "classification": v.classification,
                "scanner_name": v.scanner_name,
                "ip_address": v.ip_address,
                "user_agent": v.user_agent,
                "accept_language": v.accept_language,
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in visits
        ],
    }
