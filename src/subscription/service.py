"""Stripe subscription service."""

import logging
from datetime import datetime, timezone

import stripe
from sqlalchemy.orm import Session

from src.db.models import Subscription, TrialLink, User
from src.utils.config import get_settings

logger = logging.getLogger(__name__)


def _init_stripe() -> None:
    """Initialize Stripe with the secret key from settings."""
    settings = get_settings()
    stripe.api_key = settings.stripe_secret_key


def get_or_create_stripe_customer(db: Session, user: User) -> str:
    """Get existing Stripe customer ID or create a new one."""
    _init_stripe()

    subscription = db.query(Subscription).filter(Subscription.user_id == user.id).first()
    if subscription and subscription.stripe_customer_id:
        return subscription.stripe_customer_id

    customer = stripe.Customer.create(
        email=user.email,
        name=user.name,
        metadata={"user_id": user.id},
    )

    if not subscription:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        subscription = Subscription(
            user_id=user.id,
            stripe_customer_id=customer.id,
            plan="pro",
            status="incomplete",
            created_at=now,
        )
        db.add(subscription)
    else:
        subscription.stripe_customer_id = customer.id

    db.commit()
    return customer.id


def create_checkout_session(db: Session, user: User, billing_period: str) -> str:
    """Create a Stripe Checkout Session. Returns the checkout URL."""
    _init_stripe()
    settings = get_settings()

    # Prevent double subscription
    existing = db.query(Subscription).filter(Subscription.user_id == user.id).first()
    if existing and existing.status == "active":
        raise ValueError("already_subscribed")

    if billing_period == "monthly":
        price_id = settings.stripe_pro_monthly_price_id
    else:
        price_id = settings.stripe_pro_yearly_price_id

    if not price_id:
        raise ValueError(f"Stripe price ID not configured for {billing_period}")

    customer_id = get_or_create_stripe_customer(db, user)

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        # /checkout/success polls the backend until the subscription is
        # really active - resolves the race between the Stripe redirect and
        # the webhook that finalizes the DB row.
        success_url=f"{settings.frontend_url}/checkout/success",
        cancel_url=f"{settings.frontend_url}/pricing?canceled=true",
        metadata={"user_id": user.id},
        allow_promotion_codes=True,
    )

    return session.url


def create_portal_session(db: Session, user: User) -> str:
    """Create a Stripe Customer Portal Session. Returns the portal URL."""
    _init_stripe()
    settings = get_settings()

    subscription = db.query(Subscription).filter(Subscription.user_id == user.id).first()
    if not subscription or not subscription.stripe_customer_id:
        raise ValueError("No Stripe customer found")

    session = stripe.billing_portal.Session.create(
        customer=subscription.stripe_customer_id,
        return_url=f"{settings.frontend_url}/account?portal=return",
    )

    return session.url


def sync_subscription_from_stripe(db: Session, user: User) -> bool:
    """Sync subscription state from Stripe for a user. Returns True if an active sub was found."""
    _init_stripe()

    subscription = db.query(Subscription).filter(Subscription.user_id == user.id).first()
    if not subscription or not subscription.stripe_customer_id:
        return False

    # List active subscriptions for this customer
    try:
        subs = stripe.Subscription.list(
            customer=subscription.stripe_customer_id,
            status="all",
            limit=1,
        )
    except stripe.StripeError:
        logger.exception("Failed to list subscriptions from Stripe")
        return False

    if not subs.data:
        return False

    stripe_sub = subs.data[0]
    subscription.stripe_subscription_id = stripe_sub.id
    _sync_subscription_details(subscription, stripe_sub)
    subscription.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

    # Mark trial as converted if applicable
    if subscription.status == "active":
        trial_link = db.query(TrialLink).filter(TrialLink.user_id == user.id).first()
        if trial_link and trial_link.status not in ("converted",):
            trial_link.status = "converted"
            trial_link.converted_at = datetime.now(timezone.utc).replace(tzinfo=None)

    db.commit()
    logger.info("Synced subscription from Stripe for user %s: status=%s", user.id, subscription.status)
    return subscription.status == "active"


def handle_webhook_event(db: Session, event: stripe.Event) -> None:
    """Handle a Stripe webhook event."""
    event_type = event.type
    data = event.data.object

    logger.info("Handling Stripe webhook: %s", event_type)

    if event_type == "checkout.session.completed":
        _handle_checkout_completed(db, data)
    elif event_type == "customer.subscription.updated":
        _handle_subscription_updated(db, data)
    elif event_type == "customer.subscription.deleted":
        _handle_subscription_deleted(db, data)
    elif event_type == "invoice.payment_failed":
        _handle_payment_failed(db, data)
    elif event_type == "invoice.paid":
        _handle_payment_succeeded(db, data)
    else:
        logger.debug("Unhandled Stripe event: %s", event_type)


def _handle_checkout_completed(db: Session, session) -> None:
    """Handle checkout.session.completed - activate subscription."""
    metadata = _get(session, "metadata", {})
    user_id = _get(metadata, "user_id") if metadata else None
    if not user_id:
        logger.warning("checkout.session.completed without user_id in metadata")
        return

    stripe_subscription_id = _get(session, "subscription")
    stripe_customer_id = _get(session, "customer")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    subscription = db.query(Subscription).filter(Subscription.user_id == user_id).first()

    if subscription:
        subscription.stripe_customer_id = stripe_customer_id
        subscription.stripe_subscription_id = stripe_subscription_id
        subscription.status = "active"
        subscription.plan = "pro"
        subscription.updated_at = now
    else:
        subscription = Subscription(
            user_id=user_id,
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=stripe_subscription_id,
            plan="pro",
            status="active",
            created_at=now,
        )
        db.add(subscription)

    # Fetch subscription details for billing period
    if stripe_subscription_id:
        _init_stripe()
        try:
            stripe_sub = stripe.Subscription.retrieve(stripe_subscription_id)
            _sync_subscription_details(subscription, stripe_sub)
        except Exception:
            logger.exception("Failed to fetch subscription details")

    # Mark trial as converted if applicable
    trial_link = db.query(TrialLink).filter(TrialLink.user_id == user_id).first()
    if trial_link and trial_link.status not in ("converted",):
        trial_link.status = "converted"
        trial_link.converted_at = now

    db.commit()
    logger.info("Subscription activated for user %s", user_id)


def _handle_subscription_updated(db: Session, stripe_sub) -> None:
    """Handle customer.subscription.updated - sync status."""
    stripe_sub_id = _get(stripe_sub, "id")
    subscription = db.query(Subscription).filter(
        Subscription.stripe_subscription_id == stripe_sub_id
    ).first()
    if not subscription:
        logger.warning("Subscription not found for stripe_subscription_id=%s", stripe_sub_id)
        return

    _sync_subscription_details(subscription, stripe_sub)
    subscription.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    logger.info("Subscription updated: %s -> %s", stripe_sub_id, subscription.status)


def _handle_subscription_deleted(db: Session, stripe_sub) -> None:
    """Handle customer.subscription.deleted - mark as canceled."""
    stripe_sub_id = _get(stripe_sub, "id")
    subscription = db.query(Subscription).filter(
        Subscription.stripe_subscription_id == stripe_sub_id
    ).first()
    if not subscription:
        return

    subscription.status = "canceled"
    subscription.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    logger.info("Subscription canceled: %s", stripe_sub_id)


def _handle_payment_failed(db: Session, invoice) -> None:
    """Handle invoice.payment_failed - mark as past_due."""
    stripe_sub_id = _get(invoice, "subscription")
    if not stripe_sub_id:
        return

    subscription = db.query(Subscription).filter(
        Subscription.stripe_subscription_id == stripe_sub_id
    ).first()
    if not subscription:
        return

    subscription.status = "past_due"
    subscription.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    logger.info("Subscription past_due: %s", stripe_sub_id)


def _handle_payment_succeeded(db: Session, invoice) -> None:
    """Handle invoice.paid - confirm active."""
    stripe_sub_id = _get(invoice, "subscription")
    if not stripe_sub_id:
        return

    subscription = db.query(Subscription).filter(
        Subscription.stripe_subscription_id == stripe_sub_id
    ).first()
    if not subscription:
        return

    subscription.status = "active"
    subscription.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    logger.info("Subscription confirmed active: %s", stripe_sub_id)


def _get(obj, key, default=None):
    """Get attribute from Stripe object or dict (works with both SDK objects and webhook dicts)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _sync_subscription_details(subscription: Subscription, stripe_sub) -> None:
    """Sync subscription details from Stripe subscription object or dict."""
    status = _get(stripe_sub, "status", "active")
    # Map Stripe statuses to our statuses
    status_map = {
        "active": "active",
        "past_due": "past_due",
        "canceled": "canceled",
        "incomplete": "incomplete",
        "incomplete_expired": "canceled",
        "trialing": "active",
        "unpaid": "past_due",
        "paused": "canceled",
    }
    subscription.status = status_map.get(status, status)

    subscription.cancel_at_period_end = 1 if _get(stripe_sub, "cancel_at_period_end") else 0

    # Extract billing period from price interval
    items = _get(stripe_sub, "items", {})
    items_data = _get(items, "data", []) if items else []
    if items_data:
        item = items_data[0]
        price = _get(item, "price", {})
        recurring = _get(price, "recurring", {})
        interval = _get(recurring, "interval", "month")
        subscription.billing_period = "yearly" if interval == "year" else "monthly"

    # Period dates
    current_start = _get(stripe_sub, "current_period_start")
    current_end = _get(stripe_sub, "current_period_end")
    if current_start:
        subscription.current_period_start = datetime.fromtimestamp(current_start)
    if current_end:
        subscription.current_period_end = datetime.fromtimestamp(current_end)
