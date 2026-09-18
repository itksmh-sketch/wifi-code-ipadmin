from pydantic import BaseModel, BeforeValidator, Field, field_validator
from typing import Annotated, Literal, Optional
from datetime import datetime
from decimal import Decimal
# Single definition of the payable floor, shared with the billing jobs.
from src.modules.billing.service import PAYSTACK_MINIMUM_GHS
import uuid
from enum import Enum

from src.utils.email_address import normalize_email as _normalize_admin_email
from src.utils.phone import normalize_ghana_phone


def coerce_ip_to_str(v):
    if v is None:
        return v
    return str(v)


SafeStrIP = Annotated[str, BeforeValidator(coerce_ip_to_str)]


# --- Enums ---
class PlanType(str, Enum):
    time = "time"
    data = "data"
    hybrid = "hybrid"


class VoucherStatus(str, Enum):
    unused = "unused"
    active = "active"
    exhausted = "exhausted"
    expired = "expired"
    disabled = "disabled"


class DevicePolicy(str, Enum):
    single = "single"
    multi = "multi"


class AdminRole(str, Enum):
    superadmin = "superadmin"
    admin = "admin"
    viewer = "viewer"


# --- Shared response ---
class ErrorResponse(BaseModel):
    error: bool = True
    code: str
    message: str


# --- Auth ---
class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    # True while the admin is on a temp password: the API serves only the
    # onboarding endpoints until they verify a phone and set a password.
    must_complete_onboarding: bool = False
    # True after a platform-owner password reset: only the set-new-password step
    # is served until they choose one.
    must_change_password: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str


# --- Town ---
class TownCreate(BaseModel):
    name: str
    region: str


class TownUpdate(BaseModel):
    name: Optional[str] = None
    region: Optional[str] = None


class TownResponse(BaseModel):
    id: uuid.UUID
    name: str
    region: str
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# --- Site ---
class SiteCreate(BaseModel):
    name: str
    address: str


class SiteUpdate(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None


class SiteResponse(BaseModel):
    id: uuid.UUID
    town_id: uuid.UUID
    name: str
    address: str
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# --- Router ---
class RouterCreate(BaseModel):
    name: str
    ip_address: str
    nas_identifier: str
    nas_secret: str
    is_active: bool = True


class RouterUpdate(BaseModel):
    name: Optional[str] = None
    ip_address: Optional[str] = None
    nas_identifier: Optional[str] = None
    nas_secret: Optional[str] = None
    is_active: Optional[bool] = None


class RouterResponse(BaseModel):
    id: uuid.UUID
    site_id: uuid.UUID
    name: str
    ip_address: Optional[SafeStrIP] = None
    nas_identifier: str
    is_active: bool
    is_online: bool = False
    last_seen_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# --- Plan ---
class PlanCreate(BaseModel):
    site_id: Optional[uuid.UUID] = None
    name: str
    type: PlanType
    duration_minutes: Optional[int] = None
    data_limit_mb: Optional[int] = None
    download_speed_kbps: int
    upload_speed_kbps: int
    price_ghs: float
    is_active: bool = True


class PlanUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[PlanType] = None
    duration_minutes: Optional[int] = None
    data_limit_mb: Optional[int] = None
    download_speed_kbps: Optional[int] = None
    upload_speed_kbps: Optional[int] = None
    price_ghs: Optional[float] = None
    is_active: Optional[bool] = None


class PlanResponse(BaseModel):
    id: uuid.UUID
    site_id: Optional[uuid.UUID] = None
    name: str
    type: PlanType
    duration_minutes: Optional[int] = None
    data_limit_mb: Optional[int] = None
    download_speed_kbps: int
    upload_speed_kbps: int
    price_ghs: float
    is_active: bool
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# --- Voucher ---
class VoucherGenerate(BaseModel):
    plan_id: uuid.UUID
    site_id: Optional[uuid.UUID] = None
    quantity: int = 10
    device_policy: DevicePolicy = DevicePolicy.single
    # Number of alphanumeric characters in the code, excluding the dashes the
    # printed form is grouped with. Bounds are enforced server-side in the engine
    # (validate_code_length); None keeps the historical 16.
    code_length: Optional[int] = None


class VoucherUpdate(BaseModel):
    device_policy: Optional[DevicePolicy] = None
    max_devices: Optional[int] = None


class VoucherResponse(BaseModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    site_id: Optional[uuid.UUID] = None
    code: str
    username: str
    status: VoucherStatus
    device_policy: DevicePolicy
    max_devices: int
    activated_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    data_used_mb: int
    batch_id: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class VoucherListResponse(BaseModel):
    vouchers: list[VoucherResponse]
    total: int


# --- Session ---
class SessionResponse(BaseModel):
    id: uuid.UUID
    voucher_id: uuid.UUID
    router_id: uuid.UUID
    username: str
    mac_address: Optional[str] = None
    ip_address: Optional[str] = None
    nas_ip: Optional[str] = None
    session_id: str
    started_at: datetime
    stopped_at: Optional[datetime] = None
    terminate_cause: Optional[str] = None
    upload_bytes: int
    download_bytes: int

    model_config = {"from_attributes": True}

    @field_validator("mac_address", "ip_address", "nas_ip", mode="before")
    @classmethod
    def coerce_network_values_to_str(cls, v):
        if v is None:
            return None
        return str(v)


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]
    total: int


# --- Dashboard ---
class DashboardSummary(BaseModel):
    total_vouchers: int
    active_vouchers: int
    expired_vouchers: int
    exhausted_vouchers: int
    disabled_vouchers: int
    active_sessions: int
    total_sessions: int
    active_sites: int
    total_sites: int
    offline_routers_count: int


class PaymentMethodEnum(str, Enum):
    mtn_momo = "mtn_momo"
    vodafone_cash = "vodafone_cash"
    airteltigo = "airteltigo"
    card = "card"


class PaymentStatusEnum(str, Enum):
    pending = "pending"
    success = "success"
    failed = "failed"
    refunded = "refunded"
    reversed = "reversed"


class PaymentNextActionEnum(str, Enum):
    none = "none"
    wait = "wait"
    enter_otp = "enter_otp"
    enter_phone = "enter_phone"
    enter_pin = "enter_pin"
    enter_birthday = "enter_birthday"
    enter_address = "enter_address"
    open_url = "open_url"


class BrandingResponse(BaseModel):
    portal_display_name: Optional[str] = None
    logo_url: Optional[str] = None
    primary_color: str
    accent_color: str
    background_gradient_start: str
    welcome_message: str
    # Structural layout of the captive portal. Always populated (NULL on the
    # operator row resolves to "card_centered", today's only layout).
    template: str
    # Footer contact details. Unset (either/both) leaves the generic footer text
    # in place client-side, so these stay Optional/None rather than defaulted.
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    # True only when resolved via a settings-page preview token with an active
    # draft overlay — gates branding.js's poll loop so real customer-facing
    # portal pages (resolved via a real router token) never poll.
    is_preview: bool = False


class BrandingUpdate(BaseModel):
    # All optional: a PUT may set any subset. Passing null clears a field back to
    # the platform default. Colours are validated as #RGB / #RRGGBB hex.
    portal_display_name: Optional[str] = Field(None, max_length=120)
    primary_color: Optional[str] = Field(None, pattern=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
    accent_color: Optional[str] = Field(None, pattern=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
    background_gradient_start: Optional[str] = Field(None, pattern=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
    portal_welcome_message: Optional[str] = Field(None, max_length=500)
    portal_template: Optional[Literal["card_centered", "full_bleed"]] = None
    portal_contact_phone: Optional[str] = Field(None, max_length=64)
    portal_contact_email: Optional[str] = Field(None, max_length=255)


class PortalInitiatePaymentRequest(BaseModel):
    plan_id: uuid.UUID
    site_id: Optional[uuid.UUID] = None
    gateway: Optional[str] = None
    rt: Optional[str] = None
    phone: Optional[str] = None
    payment_method: PaymentMethodEnum


class PortalInitiatePaymentResponse(BaseModel):
    internal_reference: str
    status: PaymentStatusEnum
    next_action: PaymentNextActionEnum
    display_message: Optional[str] = None
    payment_channel: Optional[str] = None
    redirect_url: Optional[str] = None


class PortalContinuePaymentRequest(BaseModel):
    ref: str
    otp: Optional[str] = None
    phone: Optional[str] = None
    pin: Optional[str] = None
    birthday: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None


class PortalPlanSummary(BaseModel):
    id: uuid.UUID
    site_id: Optional[uuid.UUID] = None
    name: str
    type: PlanType
    duration_minutes: Optional[int] = None
    data_limit_mb: Optional[int] = None
    download_speed_kbps: int
    upload_speed_kbps: int
    price_ghs: float

    model_config = {"from_attributes": True}


class PortalPaymentStatusResponse(BaseModel):
    status: PaymentStatusEnum
    next_action: PaymentNextActionEnum
    display_message: Optional[str] = None
    voucher_code: Optional[str] = None
    failure_reason: Optional[str] = None
    payment_channel: Optional[str] = None
    plan: Optional[PortalPlanSummary] = None


class PortalAuthenticateResponse(BaseModel):
    success: bool
    username: str
    password: str


# Operator bring-your-own credentials — shared by the payment and SMS
# credential APIs (src/modules/credentials/). Category-agnostic by design:
# every field name here comes from that provider's provider_catalog
# credential_schema, so one shape serves both.
class CredentialUpsert(BaseModel):
    """Operator writes credentials for one provider. `values` is keyed by the
    provider's provider_catalog credential_schema field names."""
    values: dict[str, str]
    # None -> activate only if the operator has no active provider yet.
    activate: Optional[bool] = None


class ConfiguredProviderView(BaseModel):
    provider: str
    is_active: bool
    # Field name -> "••••1234" when stored, null when an optional field is not.
    field_hints: dict[str, Optional[str]]
    last_validated_at: Optional[datetime] = None
    last_validation_error: Optional[str] = None
    # Populated only for category="payment": the URL to give this provider's
    # dashboard for inbound payment notifications. null for SMS providers,
    # which have no operator-facing webhook concept.
    webhook_url: Optional[str] = None


class CredentialsView(BaseModel):
    active_provider: Optional[str] = None
    configured: list[ConfiguredProviderView] = Field(default_factory=list)
    # Set only on the POST /{provider}/test response: a transient one-line detail
    # from verify_credentials() (e.g. "balance GHS 0.88") to append to the
    # success message. null on every other response, and for providers with no
    # balance concept (Paystack, Flutterwave).
    test_detail: Optional[str] = None


def validate_monthly_fee(value: Optional[Decimal]) -> Optional[Decimal]:
    """A fee is either 0 (exempt) or at least Paystack's minimum (payable).

    Between those, an invoice is generated that can never be charged: it runs
    through the grace period and suspends the operator. Enforced here so the
    state is unreachable through the API, not merely skipped by the cron.
    """
    if value is None:
        return value
    if Decimal(0) < value < PAYSTACK_MINIMUM_GHS:
        raise ValueError(
            f"Monthly fee must be 0 (exempt from billing) or at least "
            f"GHS {PAYSTACK_MINIMUM_GHS} — Paystack cannot charge less, so an "
            f"invoice for GHS {value} could never be paid."
        )
    return value


class DefaultMonthlyFeeUpdate(BaseModel):
    """Body for PUT /platform/billing/default-fee — the platform-wide default a
    new operator's fee is stamped from at creation."""
    default_monthly_fee_ghs: Decimal = Field(ge=0)
    _check_fee = field_validator("default_monthly_fee_ghs")(validate_monthly_fee)


class PlatformOperatorCreate(BaseModel):
    name: str
    slug: str
    contact_email: str
    contact_phone: Optional[str] = None
    initial_admin_email: str
    # The initial admin's own mobile number. Required: their temp password is sent
    # to it and onboarding verifies it. No password is accepted — one is generated.
    initial_admin_phone: str
    # No fee here: the operator's monthly_fee_ghs is stamped server-side from the
    # platform default (billing.service.get_default_monthly_fee). Change an
    # individual operator's fee afterwards on the billing page.
    #
    # Omit trial_days for the historical behaviour — billing starts immediately.
    # Supply a day count to put the operator on a trial first, matching the
    # self-service application path.
    trial_days: Optional[int] = Field(default=None, ge=1, le=365)


    @field_validator("initial_admin_email")
    @classmethod
    def _admin_email(cls, v: str) -> str:
        return _normalize_admin_email(v)

    @field_validator("initial_admin_phone")
    @classmethod
    def _admin_phone(cls, v: str) -> str:
        return normalize_ghana_phone(v)


class PlatformOperatorBillingUpdate(BaseModel):
    """Body for PUT /platform/operators/{id}/billing.

    These were previously bare function arguments, which FastAPI bound to the
    query string — a JSON body was silently ignored.
    """
    monthly_fee_ghs: Optional[Decimal] = Field(default=None, ge=0)
    _check_fee = field_validator("monthly_fee_ghs")(validate_monthly_fee)
    extend_trial_days: Optional[int] = Field(default=None, ge=1, le=365)


class PlatformAdminCreate(BaseModel):
    """No password: one is generated and sent to phone, and the new admin
    must verify that phone and choose their own password on first sign-in."""
    email: str
    phone: str
    role: AdminRole = AdminRole.admin

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _normalize_admin_email(v)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalize_ghana_phone(v)


class PlatformAdminPasswordReset(BaseModel):
    """Only used when the admin has no verified phone: where to text the new
    temp password (stored unverified; onboarding verifies it)."""
    phone: Optional[str] = None

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        return normalize_ghana_phone(v)


class PlatformOperatorStatusUpdate(BaseModel):
    status: str


class PlatformOperatorResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    contact_email: str
    contact_phone: Optional[str] = None
    status: str
    billing_status: str
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# --- Reseller (Phase 3) ---
class ResellerVoucherPurchaseRequest(BaseModel):
    plan_id: uuid.UUID
    quantity: int


class ResellerMarkSoldRequest(BaseModel):
    sold_to_phone: str


class ResellerWalletResponse(BaseModel):
    balance_ghs: float
    lifetime_topped_up_ghs: float
    lifetime_spent_ghs: float


class ResellerWalletTransactionResponse(BaseModel):
    id: uuid.UUID
    type: str
    amount_ghs: float
    balance_after_ghs: float
    description: Optional[str] = None
    reference: str
    voucher_id: Optional[uuid.UUID] = None
    triggered_by: str
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# --- Provider catalog ---

class OperatorProviderResponse(BaseModel):
    """Operator-facing catalog row — GET /api/v1/providers?category=…

    Strict subset of ProviderCatalogEntryResponse: no id, no category, and none
    of the platform-internal state (is_integrated, is_available, sort_order).
    platform_rate_per_segment is the one exception — it's the operator's actual
    cost on a platform-provided row, so they need it to decide whether to opt
    in; it's always None on a bring-your-own row regardless of what's stored.
    """
    provider_key: str
    display_name: str
    description: Optional[str] = None
    credential_schema: dict = Field(default_factory=dict)
    is_platform_provided: bool
    platform_rate_per_segment: Optional[str] = None


# --- Provider catalog (platform owner only) ---

class ProviderCatalogEntryResponse(BaseModel):
    id: uuid.UUID
    category: str
    provider_key: str
    display_name: str
    description: Optional[str] = None
    credential_schema: dict = Field(default_factory=dict)
    is_integrated: bool
    is_available: bool
    is_platform_provided: bool
    # Serialised as a string so the 4-decimal rate survives the JSON round-trip
    # without float rounding.
    platform_rate_per_segment: Optional[str] = None
    sort_order: int

    model_config = {"from_attributes": True}


class ProviderCatalogUpdate(BaseModel):
    """Platform-admin-editable fields. Both optional — send only what changes."""
    is_available: Optional[bool] = None
    platform_rate_per_segment: Optional[Decimal] = Field(default=None, ge=0)
    # Distinguishes "leave the rate alone" (field omitted) from "clear the rate"
    # (this flag), since None already means "not sent".
    clear_platform_rate: bool = False


# --- Platform payment transactions (platform owner only) ---

class TransactionDiagnosticUpdate(BaseModel):
    is_diagnostic: bool


# --- Platform SMS credentials (platform owner only) ---

class PlatformSMSCredentialResponse(BaseModel):
    """Masked view of the platform's own Arkesel keys. Mirrors
    PlatformPaymentCredentialResponse's shape but for the single-blob
    credential store (platform_sms_credentials), not per-field columns."""
    provider: str
    api_key_masked: Optional[str] = None
    sender_id: Optional[str] = None
    is_stored: bool
    stored_updated_at: Optional[datetime] = None
    is_active: bool
    last_validated_at: Optional[datetime] = None
    last_validation_error: Optional[str] = None
    # Transient: a balance line from verify_credentials(), present only on the
    # /test response, same contract as the operator credentials test endpoint.
    test_detail: Optional[str] = None

    model_config = {"from_attributes": True}


class PlatformSMSCredentialUpdate(BaseModel):
    api_key: str
    sender_id: str
    is_active: bool = True


# --- Platform notification SMS credentials (platform owner only) ---
#
# Same shape as the gateway credential above, deliberately a distinct pair so
# the two can never be posted to the wrong endpoint by sharing a model.

class PlatformNotificationSMSCredentialResponse(BaseModel):
    provider: str
    api_key_masked: Optional[str] = None
    sender_id: Optional[str] = None
    is_stored: bool
    stored_updated_at: Optional[datetime] = None
    is_active: bool
    last_validated_at: Optional[datetime] = None
    last_validation_error: Optional[str] = None
    shares_gateway_account: bool = True
    test_detail: Optional[str] = None

    model_config = {"from_attributes": True}


class PlatformNotificationSMSCredentialUpdate(BaseModel):
    api_key: str
    sender_id: str
    is_active: bool = True
    # Stated, not detected: two API keys on one Arkesel account are
    # indistinguishable from here. Drives whether the reconciliation job treats
    # balance drift as actionable — see jobs/sms_reconciliation.
    shares_gateway_account: bool = True


# --- Platform payment credentials (platform owner only) ---

class PlatformPaymentProvider(str, Enum):
    paystack = "paystack"


class PlatformPaymentCredentialUpdate(BaseModel):
    provider: PlatformPaymentProvider = PlatformPaymentProvider.paystack
    public_key: str
    secret_key: str
    webhook_secret: Optional[str] = None
    is_active: bool = True


class PlatformPaymentCredentialResponse(BaseModel):
    """Masked read. Raw key material is never carried on this model.

    Three facts, deliberately separate — collapsing them is what made a
    stored-but-deactivated row indistinguishable from no row at all:
      * ``is_stored``     — a credential row exists
      * ``is_active``     — that row is the one in force
      * ``is_configured`` — usable keys resolved from somewhere (row or .env)
    """
    provider: str = "paystack"
    # Last-4 of the stored row when one exists, otherwise of the .env values.
    public_key_last4: Optional[str] = None
    secret_key_last4: Optional[str] = None
    webhook_secret_last4: Optional[str] = None
    is_stored: bool = False
    stored_updated_at: Optional[datetime] = None
    is_active: bool = False
    is_configured: bool = False
    # "db" when an active row supplies the keys, "env" when falling back to
    # PLATFORM_BILLING_PAYSTACK_*, so the UI can say which is actually in force.
    source: str = "env"
    last_validated_at: Optional[datetime] = None
    last_validation_error: Optional[str] = None
