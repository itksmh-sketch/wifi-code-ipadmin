from pydantic import BaseModel, BeforeValidator, field_validator
from typing import Annotated, Optional
from datetime import datetime
import uuid
from enum import Enum


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


class PaymentCredentialProvider(str, Enum):
    paystack = "paystack"


class PaymentCredentialUpdate(BaseModel):
    provider: PaymentCredentialProvider = PaymentCredentialProvider.paystack
    public_key: str
    secret_key: str
    webhook_secret: Optional[str] = None
    is_active: bool = True


class PaymentCredentialResponse(BaseModel):
    provider: str = "paystack"
    public_key_last4: Optional[str] = None
    secret_key_last4: Optional[str] = None
    webhook_secret_last4: Optional[str] = None
    is_active: bool = False
    is_configured: bool = False
    last_validated_at: Optional[datetime] = None
    last_validation_error: Optional[str] = None


class PlatformOperatorCreate(BaseModel):
    name: str
    slug: str
    contact_email: str
    contact_phone: Optional[str] = None
    initial_admin_email: str
    initial_admin_password: str


class PlatformAdminCreate(BaseModel):
    email: str
    password: str
    role: AdminRole = AdminRole.admin


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
