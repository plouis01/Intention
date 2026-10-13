"""Subscription API routes (Stripe Checkout, Portal, Status, Webhook)."""

import logging
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from src.auth.dependencies import get_current_user, get_db
from src.auth.schemas import (
    CheckoutRequest,
    CheckoutResponse,
    PortalResponse,
    SubscriptionStatusResponse,
)
from src.db.models import Subscription, TrialLink, User
from src.subscription.service import (
    create_checkout_session,
    create_portal_session,
    handle_webhook_event,
    sync_subscription_from_stripe,
)
from src.utils.config import get_settings

logger = logging.getLogger(__name__)

subscription_router = APIRouter(prefix="/subscription", tags=["subscription"])


@subscription_router.post("/checkout", response_model=CheckoutResponse)
def checkout(
    data: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a Stripe Checkout Session for subscription."""
    try:
        url = create_checkout_session(db, current_user, data.billing_period)
        return CheckoutResponse(url=url)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except stripe.StripeError as e:
        logger.exception("Stripe error during checkout")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Erreur de paiement. Veuillez réessayer.",
        )


@subscription_router.post("/portal", response_model=PortalResponse)
def portal(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a Stripe Customer Portal Session."""
    try:
        url = create_portal_session(db, current_user)
        return PortalResponse(url=url)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except stripe.StripeError as e:
        logger.exception("Stripe error during portal session")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Erreur. Veuillez réessayer.",
        )


@subscription_router.get("/status", response_model=SubscriptionStatusResponse)
def get_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get unified subscription/trial status for the current user."""
    subscription = db.query(Subscription).filter(
        Subscription.user_id == current_user.id
    ).first()

    trial_link = db.query(TrialLink).filter(
        TrialLink.user_id == current_user.id
    ).first()

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Trial info
    is_trial = False
    trial_days_remaining = None
    trial_expires_at = None
    trial_active = False
    trial_expired = False
    trial_revoked = False

    if trial_link:
        is_revoked = trial_link.status == "revoked"
        is_converted = trial_link.status == "converted"

        if not is_converted:
            is_trial = True
            trial_revoked = is_revoked

            if trial_link.expires_at:
                is_expired = trial_link.expires_at < now
                trial_expires_at = trial_link.expires_at.isoformat()
                # Ceil to match the frontend JWT fast-path (useTrialStatus.ts)
                # and avoid flicker between 6 and 7 on a freshly-issued 7-day trial.
                if is_expired:
                    trial_days_remaining = 0
                else:
                    _secs = (trial_link.expires_at - now).total_seconds()
                    trial_days_remaining = max(0, -(-int(_secs) // 86400))
                trial_active = not is_expired and not is_revoked
                trial_expired = is_expired
            else:
                # Trial not yet activated (pending link)
                trial_active = not is_revoked
                trial_expired = False

    # Subscription info (past_due gets grace period while Stripe retries)
    sub_active = subscription and subscription.status in ("active", "past_due")

    # Admin bypass
    is_admin = current_user.role == "admin"

    return SubscriptionStatusResponse(
        plan=subscription.plan if subscription else None,
        status=subscription.status if subscription else None,
        billing_period=subscription.billing_period if subscription else None,
        current_period_end=(
            subscription.current_period_end.isoformat()
            if subscription and subscription.current_period_end
            else None
        ),
        cancel_at_period_end=bool(subscription.cancel_at_period_end) if subscription else False,
        is_trial=is_trial,
        trial_days_remaining=trial_days_remaining,
        trial_expires_at=trial_expires_at,
        trial_expired=trial_expired,
        trial_revoked=trial_revoked,
        has_access=sub_active or trial_active or is_admin,
    )


@subscription_router.post("/sync")
def sync(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sync subscription state from Stripe. Used after checkout redirect when webhooks aren't available."""
    synced = sync_subscription_from_stripe(db, current_user)
    if not synced:
        return {"synced": False, "message": "No active subscription found on Stripe"}
    return {"synced": True}


@subscription_router.post("/webhook")
async def webhook(request: Request):
    """Handle Stripe webhook events."""
    settings = get_settings()
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Get a DB session manually (can't use Depends in async endpoint with sync session)
    from src.db.models import get_session

    db = get_session()
    try:
        handle_webhook_event(db, event)
    finally:
        db.close()

    return {"received": True}
