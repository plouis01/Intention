"""Pydantic schemas for authentication."""

from pydantic import BaseModel, Field


class SignUpRequest(BaseModel):
    email: str = Field(..., min_length=1)
    password: str = Field(..., min_length=8)
    name: str = Field(..., min_length=1)


class LoginRequest(BaseModel):
    email: str
    password: str


class GoogleAuthRequest(BaseModel):
    credential: str  # Google ID token


class TokenResponse(BaseModel):
    token: str
    expires_at: int


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    provider: str
    role: str
    email_verified: bool = True
    created_at: str


class VerifyEmailRequest(BaseModel):
    token: str = Field(..., min_length=1)


class RequestEmailChangeRequest(BaseModel):
    new_email: str = Field(..., min_length=1)


class VerifyEmailChangeRequest(BaseModel):
    token: str = Field(..., min_length=1)


class AuthResponse(BaseModel):
    user: UserResponse
    token: TokenResponse


class AddHistoryRequest(BaseModel):
    article_ref: str
    article_title: str


class SearchHistoryEntry(BaseModel):
    article_ref: str
    article_title: str
    searched_at: str


class SearchHistoryResponse(BaseModel):
    entries: list[SearchHistoryEntry]


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str = Field(..., min_length=8)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


class ReportArticleRequest(BaseModel):
    article_ref: str
    notify: bool = False


class ContactRequest(BaseModel):
    email: str
    message: str = Field(..., min_length=1)


class MessageRequest(BaseModel):
    message: str = Field(..., min_length=1)


class FeedbackRequest(BaseModel):
    article_ref: str
    section_index: int | None = None
    rating: str | None = None  # "up", "down", or null to clear
    message: str | None = None


# --- Trial link schemas ---


class CreateTrialLinkRequest(BaseModel):
    prospect_email: str = Field(..., min_length=1)
    prospect_name: str | None = None


class CreateTrialLinkBatchRequest(BaseModel):
    emails: list[str] = Field(..., min_length=1)


class RedeemTrialRequest(BaseModel):
    token: str = Field(..., min_length=1)


class TrialLinkResponse(BaseModel):
    id: int
    token: str
    prospect_email: str
    prospect_name: str | None
    status: str
    created_at: str
    expires_at: str | None  # None until trial is activated (first redemption)
    link_expires_at: str  # Link validity deadline (before first open)
    redeemed_at: str | None
    converted_at: str | None
    visit_count: int
    search_count: int
    last_activity: str | None
    link_url: str


class TrialStatusResponse(BaseModel):
    is_trial: bool
    days_remaining: int
    expires_at: str | None
    is_expired: bool
    is_revoked: bool
    can_search: bool


class TrialInspectResponse(BaseModel):
    prospect_email: str
    prospect_name: str | None = None
    link_expires_at: str
    trial_duration_days: int
    already_activated: bool = False
    is_converted: bool = False


class TrialVisitResponse(BaseModel):
    id: int
    event_type: str  # "inspect" | "redeem"
    classification: str  # "human" | "bot" | "unknown"
    scanner_name: str | None
    ip_address: str | None
    user_agent: str | None
    accept_language: str | None
    created_at: str


# --- Subscription schemas ---


class CheckoutRequest(BaseModel):
    billing_period: str = Field(..., pattern="^(monthly|yearly)$")


class CheckoutResponse(BaseModel):
    url: str


class PortalResponse(BaseModel):
    url: str


class SubscriptionStatusResponse(BaseModel):
    plan: str | None  # "pro" or None
    status: str | None  # "active", "canceled", "past_due", "incomplete" or None
    billing_period: str | None  # "monthly", "yearly" or None
    current_period_end: str | None
    cancel_at_period_end: bool
    is_trial: bool
    trial_days_remaining: int | None
    trial_expires_at: str | None
    trial_expired: bool
    trial_revoked: bool
    has_access: bool  # True if active subscription OR active trial
