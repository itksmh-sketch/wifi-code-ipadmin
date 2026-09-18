from datetime import datetime, timedelta, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, nulls_last, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from decimal import Decimal
from typing import Optional
from src.db.models import (
    AdminUser,
    ISPOperator,
    OperatorBillingEvent,
    OperatorInvoice,
    OperatorPaymentCredential,
    PaymentTransaction,
    PlatformOwner,
    PlatformPaymentCredential,
    PlatformNotificationSMSCredential,
    PlatformSMSCredential,
    Plan,
    ProviderCatalogEntry,
    Router,
    RouterCredential,
    RouterSetupStatus,
    SMSUsageRecord,
    Session,
    Site,
    Town,
    Voucher,
)
import logging

from fastapi import Body
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.middleware.auth import get_platform_owner_context, token_version_matches
from src.modules.admin_accounts.passwords import password_policy_error
from src.modules.auth.tokens import platform_owner_token_response
from src.utils.email_address import normalize_email
from src.utils.payload import parse_update as _parse_update, reject_unknown_fields as _reject_unknown_fields
from src.utils.phone import GHANA_PHONE_ERROR, normalize_ghana_phone
from src.modules.mikrotik import setup_status as setup_store
# Reuse the admin view's reachability predicate verbatim so the platform
# drill-down can never disagree with what an operator's own dashboard shows.
from src.modules.mikrotik.setup_routes import _is_online as router_is_online
from src.middleware.rate_limit import enforce_rate_limit
from src.modules.admin_accounts import platform_reset
from src.modules.admin_accounts.notifications import send_temp_password_sms
from src.modules.admin_accounts.provisioning import admin_email_taken, provision_operator_admin
from src.modules.sms.types import SMSSendResult
from src.utils.phone import mask_phone
from src.schemas import DefaultMonthlyFeeUpdate, LoginRequest, PlatformAdminCreate, PlatformAdminPasswordReset, PlatformOperatorBillingUpdate, PlatformOperatorCreate, PlatformOperatorStatusUpdate, PlatformPaymentCredentialResponse, PlatformPaymentCredentialUpdate, PlatformNotificationSMSCredentialResponse, PlatformNotificationSMSCredentialUpdate, PlatformSMSCredentialResponse, PlatformSMSCredentialUpdate, ProviderCatalogEntryResponse, ProviderCatalogUpdate, RefreshRequest, TokenResponse, TransactionDiagnosticUpdate
from src.modules.billing.service import DEFAULT_MONTHLY_FEE_KEY, get_default_monthly_fee
from src.modules.payments.filters import REAL_TRANSACTIONS_ONLY
from src.utils.encryption import encrypt_secret
from src.utils.auth import (
    hash_password,
    verify_password,
    verify_platform_owner_token,
)

router = APIRouter(prefix="/platform", tags=["platform"])

VALID_OPERATOR_STATUSES = {"pending", "approved", "suspended", "cancelled"}


def _provisioned_admin_payload(admin: AdminUser, temp_password: str, sms_result: SMSSendResult) -> dict:
    """What the platform owner sees once, right after creating an admin."""
    return {
        "id": str(admin.id),
        "email": admin.email,
        "role": admin.role,
        "phone": mask_phone(admin.phone),
        "temp_password": temp_password,
        "temp_password_sms_sent": sms_result.success,
        "temp_password_sms_error": sms_result.error,
    }


def _month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@router.post("/auth/login", response_model=TokenResponse)
async def platform_auth_login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "platform:login", limit=10, window_seconds=60)
    result = await db.execute(select(PlatformOwner).where(PlatformOwner.email == body.email, PlatformOwner.is_active == True))
    owner = result.scalar_one_or_none()
    if not owner or not verify_password(body.password, owner.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    owner.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    return platform_owner_token_response(owner)


@router.post("/auth/refresh", response_model=TokenResponse)
async def platform_auth_refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    payload = verify_platform_owner_token(body.refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    owner_id = payload.get("sub")
    result = await db.execute(select(PlatformOwner).where(PlatformOwner.id == owner_id, PlatformOwner.is_active == True))
    owner = result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Platform owner not found or inactive")
    if not token_version_matches(payload, owner):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    return platform_owner_token_response(owner)


async def _operator_row(db: AsyncSession, operator: ISPOperator) -> dict:
    start = _month_start()
    admin_count = (
        await db.execute(
            select(func.count()).select_from(AdminUser).where(AdminUser.isp_operator_id == operator.id)
        )
    ).scalar() or 0
    voucher_count = (
        await db.execute(
            select(func.count()).select_from(Voucher).where(
                Voucher.isp_operator_id == operator.id,
                Voucher.created_at >= start,
            )
        )
    ).scalar() or 0
    monthly_revenue = (
        await db.execute(
            select(func.coalesce(func.sum(PaymentTransaction.amount_ghs), 0)).where(
                PaymentTransaction.isp_operator_id == operator.id,
                PaymentTransaction.status == "success",
                PaymentTransaction.completed_at >= start,
                REAL_TRANSACTIONS_ONLY,
            )
        )
    ).scalar() or 0
    return {
        "id": str(operator.id),
        "name": operator.name,
        "slug": operator.slug,
        "contact_email": operator.contact_email,
        "contact_phone": operator.contact_phone,
        "status": operator.status,
        "billing_status": operator.billing_status,
        "created_at": operator.created_at,
        "admin_count": admin_count,
        "voucher_count_this_month": voucher_count,
        "monthly_revenue_this_month": float(monthly_revenue),
    }


@router.get("/me")
async def platform_me(owner: PlatformOwner = Depends(get_platform_owner_context)):
    return {
        "id": str(owner.id),
        "email": owner.email,
        "name": owner.name,
        "last_login_at": owner.last_login_at,
    }


logger = logging.getLogger("platform.self_service")


def _clean_name(value: str) -> str:
    """Validate what actually gets stored: a whitespace-only name passes a bare
    min_length check and then strips to empty."""
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("cannot be blank")
    return cleaned


class PlatformOwnerProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=255)

    _strip_name = field_validator("name")(_clean_name)


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)
    confirm_password: str = Field(max_length=256)


class OperatorProfileUpdate(BaseModel):
    """Identity fields only. Not editable here: slug (unique, appears in webhook
    URLs), billing, status and credentials — each has its own endpoint."""

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, max_length=255)
    # Reaches Paystack as the payer email when an operator invoice is charged,
    # and is where billing/trial notifications go, so it must be a real address.
    contact_email: Optional[str] = Field(default=None, max_length=255)
    contact_phone: Optional[str] = Field(default=None, max_length=64)

    @field_validator("name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        return _clean_name(v) if v is not None else v


def _validated_password(admin_or_owner, body: PasswordChange, verify) -> str:
    """Shared password-change checks for both account types."""
    if not verify(body.current_password, admin_or_owner.password_hash):
        raise HTTPException(status_code=400, detail="Your current password is incorrect.")
    error = password_policy_error(body.new_password)
    if error:
        raise HTTPException(status_code=400, detail=error)
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=400, detail="The two passwords don't match.")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="Choose a password different from your current one.")
    return body.new_password


@router.patch("/me")
async def update_platform_me(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Edit your own display name. Email is the login identifier and is immutable."""
    _reject_unknown_fields(payload, {"name"})
    body = _parse_update(PlatformOwnerProfileUpdate, payload)
    owner.name = body.name
    await db.commit()
    await db.refresh(owner)
    logger.info("platform_owner_profile_updated owner_id=%s", owner.id)
    return {"id": str(owner.id), "email": owner.email, "name": owner.name}


@router.post("/me/password", response_model=TokenResponse)
async def change_platform_me_password(
    body: PasswordChange,
    request: Request,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Change your own password. Every other session dies immediately; the fresh
    token pair in this response keeps the caller signed in."""
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "platform:password-change", limit=10, window_seconds=900)
    await enforce_rate_limit(f"owner:{owner.id}", "platform:password-change", limit=5, window_seconds=900)

    new_password = _validated_password(owner, body, verify_password)
    owner.password_hash = hash_password(new_password)
    owner.token_version = int(owner.token_version or 0) + 1
    await db.commit()
    await db.refresh(owner)
    logger.warning("platform_owner_password_changed owner_id=%s", owner.id)
    return platform_owner_token_response(owner)


@router.patch("/operators/{operator_id}")
async def update_operator_profile(
    operator_id: uuid.UUID,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Edit an operator's business name and billing contact details."""
    _reject_unknown_fields(payload, {"name", "contact_email", "contact_phone"})
    body = _parse_update(OperatorProfileUpdate, payload)

    operator = await db.get(ISPOperator, operator_id)
    if not operator:
        raise HTTPException(status_code=404, detail="Operator not found")

    changed = []
    if "name" in payload:
        operator.name = body.name
        changed.append("name")
    if "contact_email" in payload:
        try:
            email = normalize_email(body.contact_email or "")
        except ValueError:
            raise HTTPException(status_code=400, detail="Enter a valid contact email address.")
        # Not unique in the schema, but a duplicate is almost always a mistake.
        clash = (
            await db.execute(
                select(ISPOperator.id).where(
                    func.lower(ISPOperator.contact_email) == email, ISPOperator.id != operator.id
                )
            )
        ).first()
        if clash:
            raise HTTPException(status_code=409, detail="Another operator already uses that contact email.")
        operator.contact_email = email
        changed.append("contact_email")
    if "contact_phone" in payload:
        raw = (body.contact_phone or "").strip()
        if raw:
            try:
                operator.contact_phone = normalize_ghana_phone(raw)
            except ValueError:
                raise HTTPException(status_code=400, detail=GHANA_PHONE_ERROR)
        else:
            operator.contact_phone = None  # explicit clear
        changed.append("contact_phone")

    await db.commit()
    await db.refresh(operator)
    logger.info(
        "operator_profile_updated operator_id=%s by_platform_owner=%s fields=%s",
        operator.id, owner.id, ",".join(changed),
    )
    return await _operator_row(db, operator)


@router.get("/operators")
async def list_operators(
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    result = await db.execute(select(ISPOperator).order_by(ISPOperator.created_at.desc()))
    return [await _operator_row(db, operator) for operator in result.scalars().all()]


@router.post("/operators", status_code=status.HTTP_201_CREATED)
async def create_operator(
    body: PlatformOperatorCreate,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    existing = (
        await db.execute(
            select(ISPOperator).where(
                or_(ISPOperator.slug == body.slug, ISPOperator.contact_email == str(body.contact_email))
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Operator slug or contact email already exists")

    if await admin_email_taken(db, body.initial_admin_email):
        raise HTTPException(status_code=409, detail="Initial admin email already exists")

    now = datetime.now(timezone.utc)
    on_trial = body.trial_days is not None
    # Stamped from the platform default — never client-supplied. The resolver
    # guarantees a value the isp_operators CHECK accepts (0 or >= GHS 1.00).
    monthly_fee_ghs = await get_default_monthly_fee(db)
    operator = ISPOperator(
        name=body.name,
        slug=body.slug,
        contact_email=str(body.contact_email),
        contact_phone=body.contact_phone,
        status="approved",
        approved_at=now,
        approved_by_platform_owner_id=owner.id,
        monthly_fee_ghs=monthly_fee_ghs,
        billing_status="trial" if on_trial else "active",
        trial_ends_at=(now + timedelta(days=body.trial_days)) if on_trial else None,
    )
    db.add(operator)
    await db.flush()

    if on_trial:
        db.add(
            OperatorBillingEvent(
                isp_operator_id=operator.id,
                event_type="trial_started",
                description=f"Trial started for {operator.name}. Ends {operator.trial_ends_at.date()}.",
                event_metadata={"trial_days": body.trial_days, "monthly_fee_ghs": str(monthly_fee_ghs)},
            )
        )

    admin, temp_password = await provision_operator_admin(
        db,
        operator_id=operator.id,
        email=body.initial_admin_email,
        phone=body.initial_admin_phone,
        role="superadmin",
    )
    await db.commit()
    await db.refresh(operator)
    # Only after the commit: never text credentials for an account that rolled back.
    sms_result = await send_temp_password_sms(admin, temp_password)
    row = await _operator_row(db, operator)
    row["initial_admin"] = _provisioned_admin_payload(admin, temp_password, sms_result)
    return row


@router.get("/operators/{operator_id}")
async def get_operator(
    operator_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    operator = await db.get(ISPOperator, operator_id)
    if not operator:
        raise HTTPException(status_code=404, detail="Operator not found")
    row = await _operator_row(db, operator)
    creds = (
        await db.execute(
            select(OperatorPaymentCredential).where(OperatorPaymentCredential.isp_operator_id == operator.id)
        )
    ).scalar_one_or_none()
    row["payment_credentials"] = {
        "configured": bool(creds),
        "active": bool(creds and creds.is_active),
        "last_validated_at": creds.last_validated_at if creds else None,
        "last_validation_error": creds.last_validation_error if creds else None,
    }
    # The detail page renders total_sessions; without this it only ever came from
    # /summary, so the page showed 0. Kept out of _operator_row so the list
    # endpoint doesn't pick up another per-operator query.
    row["total_sessions"] = (
        await db.execute(select(func.count()).select_from(Session).where(Session.isp_operator_id == operator.id))
    ).scalar() or 0
    return row


@router.put("/operators/{operator_id}/status")
async def update_operator_status(
    operator_id: uuid.UUID,
    body: PlatformOperatorStatusUpdate,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    if body.status not in VALID_OPERATOR_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid operator status")
    operator = await db.get(ISPOperator, operator_id)
    if not operator:
        raise HTTPException(status_code=404, detail="Operator not found")

    previous_status = operator.status
    operator.status = body.status
    operator.updated_at = datetime.now(timezone.utc)
    if body.status == "approved" and not operator.approved_at:
        operator.approved_at = datetime.now(timezone.utc)
        operator.approved_by_platform_owner_id = owner.id
    if body.status == "cancelled":
        operator.billing_status = "cancelled"

    # A suspension the platform owner imposed is NOT reversible by paying an
    # invoice. Tagging it is what stops the billing webhook from lifting it.
    if body.status == "suspended":
        operator.suspension_reason = "manual"
    elif previous_status == "suspended":
        operator.suspension_reason = None

    # Manual status changes previously left no trace at all, so the audit trail
    # covered only billing-driven suspensions — half the story, and the half you
    # least need when explaining why an operator's status changed.
    if body.status == "suspended" and previous_status != "suspended":
        db.add(OperatorBillingEvent(
            isp_operator_id=operator.id,
            event_type="suspended",
            description=f"{operator.name} suspended by platform owner.",
            event_metadata={"reason": "manual", "suspended_by_platform_owner_id": str(owner.id)},
        ))
    elif previous_status == "suspended" and body.status != "suspended":
        db.add(OperatorBillingEvent(
            isp_operator_id=operator.id,
            event_type="reactivated",
            description=f"{operator.name} un-suspended by platform owner (status set to {body.status}).",
            event_metadata={"reason": "manual", "reactivated_by_platform_owner_id": str(owner.id)},
        ))

    await db.commit()
    await db.refresh(operator)
    return await _operator_row(db, operator)


@router.get("/operators/{operator_id}/admins")
async def list_operator_admins(
    operator_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    if not await db.get(ISPOperator, operator_id):
        raise HTTPException(status_code=404, detail="Operator not found")
    result = await db.execute(
        select(AdminUser).where(AdminUser.isp_operator_id == operator_id).order_by(AdminUser.created_at.desc())
    )
    return [
        {
            "id": str(admin.id),
            "email": admin.email,
            "role": admin.role,
            "is_active": bool(admin.is_active),
            "created_at": admin.created_at,
            "last_login_at": admin.last_login_at,
            "phone": mask_phone(admin.phone),
            "phone_verified": bool(admin.phone_verified),
            "must_complete_onboarding": bool(admin.must_complete_onboarding),
            "must_change_password": bool(admin.must_change_password),
        }
        for admin in result.scalars().all()
    ]


@router.post("/operators/{operator_id}/admins/{admin_id}/reset-password")
async def reset_operator_admin_password(
    operator_id: uuid.UUID,
    admin_id: uuid.UUID,
    body: PlatformAdminPasswordReset,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Reset an operator admin's password. Logs them out everywhere immediately.

    Scoped to operator admins: the target must be an admin_users row belonging to
    this operator (platform-owner accounts live in a different table and can
    never match). See admin_accounts.platform_reset for the two outcomes.
    """
    # Per target admin, so even an authorised platform account can't spam
    # resets (each one logs the admin out and texts them).
    await enforce_rate_limit(
        f"admin:{admin_id}",
        "platform:admin-password-reset",
        limit=platform_reset.RESET_RATE_LIMIT,
        window_seconds=platform_reset.RESET_RATE_WINDOW_SECONDS,
    )
    admin = (
        await db.execute(
            select(AdminUser)
            .where(AdminUser.id == admin_id, AdminUser.isp_operator_id == operator_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if admin is None:
        raise HTTPException(status_code=404, detail="Admin not found for this operator")

    try:
        outcome = await platform_reset.reset_admin_password(
            db, admin=admin, platform_owner_id=owner.id, phone=body.phone
        )
    except platform_reset.VerifiedPhoneConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "admin_id": str(admin.id),
        "email": admin.email,
        "mode": outcome.mode,
        "sessions_revoked": True,
        "phone": mask_phone(admin.phone),
        "phone_verified": bool(admin.phone_verified),
        "phone_changed": outcome.phone_changed,
        "sms_sent": outcome.sms_sent,
        "sms_error": outcome.sms_error,
        # Only when the admin goes back through onboarding (unverified phone).
        "temp_password": outcome.temp_password,
        "is_active": bool(admin.is_active),
        "event_id": str(outcome.event_id),
    }


@router.post("/operators/{operator_id}/admins", status_code=status.HTTP_201_CREATED)
async def create_operator_admin(
    operator_id: uuid.UUID,
    body: PlatformAdminCreate,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    if not await db.get(ISPOperator, operator_id):
        raise HTTPException(status_code=404, detail="Operator not found")
    if await admin_email_taken(db, body.email):
        raise HTTPException(status_code=409, detail="Admin email already exists")
    admin, temp_password = await provision_operator_admin(
        db, operator_id=operator_id, email=body.email, phone=body.phone, role=body.role.value
    )
    await db.commit()
    await db.refresh(admin)
    # Only after the commit: never text credentials for an account that rolled back.
    sms_result = await send_temp_password_sms(admin, temp_password)
    return {
        **_provisioned_admin_payload(admin, temp_password, sms_result),
        "is_active": bool(admin.is_active),
        "created_at": admin.created_at,
        "last_login_at": admin.last_login_at,
    }


@router.get("/operators/{operator_id}/summary")
async def operator_summary(
    operator_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    operator = await db.get(ISPOperator, operator_id)
    if not operator:
        raise HTTPException(status_code=404, detail="Operator not found")
    row = await _operator_row(db, operator)
    total_sessions = (
        await db.execute(select(func.count()).select_from(Session).where(Session.isp_operator_id == operator.id))
    ).scalar() or 0
    row["total_sessions"] = total_sessions
    return row


# ---------------------------------------------------------------------------
# Operator infrastructure drill-down (read-only)
# ---------------------------------------------------------------------------
# Platform-owner scope only, and deliberately cross-tenant: the owner sees every
# operator's estate. Strictly a projection — no writes, no router contact, and
# no field of the Router model is ever serialized wholesale (nas_secret /
# nas_secret_plain must never leave the API), so every payload below is built
# key-by-key from an explicit allow-list.


def _ip_str(value) -> Optional[str]:
    """INET columns come back as str or ipaddress objects depending on driver."""
    return str(value) if value is not None else None


def _router_payload(router: Router, setup: Optional[RouterSetupStatus], cred: Optional[RouterCredential]) -> dict:
    tunnel_ip = _ip_str(router.wg_tunnel_ip)
    label_ip = _ip_str(router.ip_address)
    if router.wg_enabled and tunnel_ip:
        connectivity = "tunnel"
    elif label_ip:
        connectivity = "direct"
    else:
        connectivity = "none"
    sections = {
        name: {
            "status": getattr(setup, f"{name}_status", None) or "unconfigured",
            "applied_at": getattr(setup, f"{name}_applied_at", None),
        }
        for name in setup_store.SECTIONS
    }
    sections_complete = setup_store.sections_complete(setup)
    return {
        "id": str(router.id),
        "name": router.name,
        "nas_identifier": router.nas_identifier,
        "is_active": bool(router.is_active),
        # `online` is the single source of truth for the UI badge; the two raw
        # flags are exposed alongside it so the owner can see *why*.
        "online": router_is_online(router),
        "is_online": bool(router.is_online),
        "last_seen_at": router.last_seen_at,
        "connectivity": connectivity,
        "wg_enabled": bool(router.wg_enabled),
        "wg_is_connected": bool(router.wg_is_connected),
        "wg_tunnel_ip": tunnel_ip,
        "wg_last_handshake_at": router.wg_last_handshake_at,
        "ip_address": label_ip,
        "setup": {
            # No setup_status row yet == never provisioned, not an error state.
            "tracked": setup is not None,
            "sections_complete": sections_complete,
            "sections_total": len(setup_store.SECTIONS),
            "provisioned": sections_complete == len(setup_store.SECTIONS),
            **sections,
        },
        "api_credentials": {
            "configured": cred is not None,
            "connection_status": cred.connection_status if cred else "unknown",
            "last_connected_at": cred.last_connected_at if cred else None,
        },
    }


def _plan_payload(plan: Plan, site_name: Optional[str]) -> dict:
    return {
        "id": str(plan.id),
        "name": plan.name,
        "type": plan.type,
        "duration_minutes": plan.duration_minutes,
        "data_limit_mb": plan.data_limit_mb,
        "download_speed_kbps": plan.download_speed_kbps,
        "upload_speed_kbps": plan.upload_speed_kbps,
        "price_ghs": float(plan.price_ghs or 0),
        "is_active": bool(plan.is_active),
        "site_id": str(plan.site_id) if plan.site_id else None,
        # plans.site_id is nullable — a null means the plan is offered estate-wide.
        "site_name": site_name,
        "scope": "site" if plan.site_id else "operator-wide",
        "created_at": plan.created_at,
    }


@router.get("/operators/{operator_id}/infrastructure")
async def operator_infrastructure(
    operator_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    """Towns -> sites -> routers, plus the operator's plans, in 4 flat queries.

    The tree is grouped in memory rather than walked with per-parent queries, so
    the query count is constant no matter how many towns/sites/routers exist.
    """
    operator = await db.get(ISPOperator, operator_id)
    if not operator:
        raise HTTPException(status_code=404, detail="Operator not found")

    # 1) towns
    towns = (
        await db.execute(
            select(Town).where(Town.isp_operator_id == operator_id).order_by(Town.name)
        )
    ).scalars().all()

    # 2) sites
    sites = (
        await db.execute(
            select(Site).where(Site.isp_operator_id == operator_id).order_by(Site.name)
        )
    ).scalars().all()

    # 3) routers + their setup/credential side-tables in one pass (outer joins so
    #    a router that was added but never provisioned still comes back).
    router_rows = (
        await db.execute(
            select(Router, RouterSetupStatus, RouterCredential)
            .outerjoin(RouterSetupStatus, RouterSetupStatus.router_id == Router.id)
            .outerjoin(RouterCredential, RouterCredential.router_id == Router.id)
            .where(Router.isp_operator_id == operator_id)
            .order_by(Router.name)
        )
    ).all()

    # 4) plans + the site label for site-scoped ones
    plan_rows = (
        await db.execute(
            select(Plan, Site.name)
            .outerjoin(Site, Site.id == Plan.site_id)
            .where(Plan.isp_operator_id == operator_id)
            .order_by(Plan.price_ghs, Plan.name)
        )
    ).all()

    routers_by_site: dict[str, list] = {}
    routers_online = 0
    for router_row, setup_row, cred_row in router_rows:
        payload = _router_payload(router_row, setup_row, cred_row)
        if payload["online"]:
            routers_online += 1
        routers_by_site.setdefault(str(router_row.site_id), []).append(payload)

    sites_by_town: dict[str, list] = {}
    orphan_sites: list = []
    town_ids = {str(town.id) for town in towns}
    for site in sites:
        site_payload = {
            "id": str(site.id),
            "name": site.name,
            "address": site.address,
            "created_at": site.created_at,
            "routers": routers_by_site.pop(str(site.id), []),
        }
        town_key = str(site.town_id) if site.town_id else None
        if town_key in town_ids:
            sites_by_town.setdefault(town_key, []).append(site_payload)
        else:
            # Town missing/mismatched — surface the site rather than dropping it.
            orphan_sites.append(site_payload)

    town_payloads = []
    for town in towns:
        town_sites = sites_by_town.get(str(town.id), [])
        town_payloads.append(
            {
                "id": str(town.id),
                "name": town.name,
                "region": town.region,
                "created_at": town.created_at,
                "site_count": len(town_sites),
                "router_count": sum(len(s["routers"]) for s in town_sites),
                "sites": town_sites,
            }
        )

    # Anything left in routers_by_site points at a site this operator doesn't own.
    orphan_routers = [r for group in routers_by_site.values() for r in group]

    plans = [_plan_payload(plan, site_name) for plan, site_name in plan_rows]

    return {
        "operator": {
            "id": str(operator.id),
            "name": operator.name,
            "slug": operator.slug,
            "status": operator.status,
        },
        "towns": town_payloads,
        "unassigned_sites": orphan_sites,
        "unassigned_routers": orphan_routers,
        "plans": plans,
        "totals": {
            "towns": len(town_payloads),
            "sites": len(sites),
            "routers": len(router_rows),
            "routers_online": routers_online,
            "routers_offline": len(router_rows) - routers_online,
            "plans": len(plans),
            "plans_active": sum(1 for p in plans if p["is_active"]),
        },
    }


# ---------------------------------------------------------------------------
# Platform billing endpoints
#
# Consumed by the vanilla platform-billing page at /platform/billing (feature
# #3, Phase 5): /billing/summary + /billing/operators feed the summary strip and
# operators table, /billing/invoices the paginated invoices table,
# /operators/{id}/billing the inline monthly-fee editor, and /invoices/{id}/waive
# the waive action.
#
# The original consumer was the React page frontend/src/pages/platform/
# PlatformBilling.jsx (summary + per-operator table, waive never implemented —
# it alerted "use the API directly"), deleted when the React platform-owner
# portal was retired in favour of the vanilla portal; see git history.
# ---------------------------------------------------------------------------

_INVOICES_PAGE_SIZE_MAX = 200

@router.get("/billing/summary")
async def platform_billing_summary(
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    month_start = _month_start()
    total_active = (
        await db.execute(select(func.count()).select_from(ISPOperator).where(ISPOperator.status == "approved"))
    ).scalar() or 0
    on_trial = (
        await db.execute(select(func.count()).select_from(ISPOperator).where(ISPOperator.billing_status == "trial"))
    ).scalar() or 0
    overdue = (
        await db.execute(
            select(func.count()).select_from(ISPOperator).where(ISPOperator.billing_status == "past_due")
        )
    ).scalar() or 0
    mrr = (
        await db.execute(
            select(func.coalesce(func.sum(ISPOperator.monthly_fee_ghs), 0)).where(
                ISPOperator.billing_status == "active"
            )
        )
    ).scalar() or Decimal("0")
    collected = (
        await db.execute(
            select(func.coalesce(func.sum(OperatorInvoice.amount_ghs), 0)).where(
                OperatorInvoice.status == "paid",
                OperatorInvoice.paid_at >= month_start,
            )
        )
    ).scalar() or Decimal("0")
    # Same "unpaid" definition the per-operator /billing/operators query uses.
    total_outstanding = (
        await db.execute(
            select(func.coalesce(func.sum(OperatorInvoice.amount_ghs), 0)).where(
                OperatorInvoice.status.in_(["issued", "overdue"])
            )
        )
    ).scalar() or Decimal("0")
    return {
        "total_active_operators": total_active,
        "operators_on_trial": on_trial,
        "operators_overdue": overdue,
        "monthly_recurring_revenue_ghs": float(mrr),
        "revenue_collected_this_month_ghs": float(collected),
        "total_outstanding_ghs": float(total_outstanding),
    }


@router.get("/billing/default-fee")
async def get_billing_default_fee(
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    """The platform-wide default monthly fee. New operators are created with
    this value (both onboarding paths); existing operators are unaffected by a
    change here — edit those on this page's operators table."""
    return {"default_monthly_fee_ghs": float(await get_default_monthly_fee(db))}


@router.put("/billing/default-fee")
async def set_billing_default_fee(
    body: DefaultMonthlyFeeUpdate,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    """Set the default. Validated to 0 or >= GHS 1.00 (validate_monthly_fee), so
    the stored value is always one an operator's CHECK constraint accepts."""
    from src.modules.platform.settings_service import set_setting

    await set_setting(db, DEFAULT_MONTHLY_FEE_KEY, f"{body.default_monthly_fee_ghs:.2f}")
    await db.commit()
    return {"default_monthly_fee_ghs": float(await get_default_monthly_fee(db))}


@router.get("/billing/operators")
async def platform_billing_operators(
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    operators = (await db.execute(select(ISPOperator).order_by(ISPOperator.name))).scalars().all()

    # An operator can carry several unpaid invoices at once, so these are
    # aggregated rather than fetched as a single row — reading one invoice with
    # scalar_one_or_none() raised MultipleResultsFound and 500'd the endpoint.
    outstanding_rows = (
        await db.execute(
            select(
                OperatorInvoice.isp_operator_id,
                func.count().label("count"),
                func.coalesce(func.sum(OperatorInvoice.amount_ghs), 0).label("total"),
                func.min(OperatorInvoice.due_at).label("next_due_at"),
            )
            .where(OperatorInvoice.status.in_(["issued", "overdue"]))
            .group_by(OperatorInvoice.isp_operator_id)
        )
    ).all()
    outstanding_by_operator = {row.isp_operator_id: row for row in outstanding_rows}

    # The oldest unpaid invoice is the one the operator is chased for, so it is
    # the one worth naming on the row.
    oldest_rows = (
        await db.execute(
            select(OperatorInvoice)
            .where(OperatorInvoice.status.in_(["issued", "overdue"]))
            .order_by(OperatorInvoice.isp_operator_id, OperatorInvoice.created_at.asc())
        )
    ).scalars().all()
    oldest_by_operator: dict = {}
    for invoice in oldest_rows:
        oldest_by_operator.setdefault(invoice.isp_operator_id, invoice)

    last_paid_rows = (
        await db.execute(
            select(
                OperatorInvoice.isp_operator_id,
                func.max(OperatorInvoice.paid_at).label("last_paid_at"),
            )
            .where(OperatorInvoice.status == "paid")
            .group_by(OperatorInvoice.isp_operator_id)
        )
    ).all()
    last_paid_by_operator = {row.isp_operator_id: row.last_paid_at for row in last_paid_rows}

    rows = []
    for op in operators:
        outstanding = outstanding_by_operator.get(op.id)
        oldest = oldest_by_operator.get(op.id)
        last_paid_at = last_paid_by_operator.get(op.id)
        rows.append({
            "id": str(op.id),
            "name": op.name,
            "slug": op.slug,
            "billing_status": op.billing_status,
            "monthly_fee_ghs": float(op.monthly_fee_ghs),
            "trial_ends_at": op.trial_ends_at.isoformat() if op.trial_ends_at else None,
            "last_paid_at": last_paid_at.isoformat() if last_paid_at else None,
            "next_due_at": outstanding.next_due_at.isoformat() if outstanding and outstanding.next_due_at else None,
            # Total across every unpaid invoice, not just the oldest one.
            "outstanding_amount_ghs": float(outstanding.total) if outstanding else 0,
            "outstanding_invoice_count": int(outstanding.count) if outstanding else 0,
            "outstanding_invoice_number": oldest.invoice_number if oldest else None,
        })
    return rows


@router.get("/billing/invoices")
async def platform_billing_invoices(
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    """Paginated list of every operator invoice, newest first — feeds the
    platform billing page's invoices table.

    Ordered by issued_at DESC (NULLS LAST), then id DESC. issued_at is stamped
    once per invoice in create_invoice(); created_at is func.now() (transaction
    time) and is identical for a whole cron batch, so it cannot order within
    one. id is the unique final tiebreaker, making the order total and
    reproducible.

    `page` floors at 1; `page_size` is clamped to 1..200 and never trusts the
    client. A `page` past the last one returns an empty list — the normal
    "paged past the end" case — not an error.

    There is deliberately no create endpoint: invoice numbering is not safe
    against a manual call racing the monthly cron (feature #3).
    """
    page = max(1, page)
    page_size = max(1, min(page_size, _INVOICES_PAGE_SIZE_MAX))

    total_count = (
        await db.execute(select(func.count()).select_from(OperatorInvoice))
    ).scalar() or 0
    total_pages = max(1, (total_count + page_size - 1) // page_size)

    rows = (
        await db.execute(
            select(OperatorInvoice, ISPOperator.name, ISPOperator.slug)
            .join(ISPOperator, OperatorInvoice.isp_operator_id == ISPOperator.id)
            .order_by(
                nulls_last(OperatorInvoice.issued_at.desc()),
                OperatorInvoice.id.desc(),
            )
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()

    return {
        "invoices": [
            {
                "id": str(inv.id),
                "invoice_number": inv.invoice_number,
                "operator_id": str(inv.isp_operator_id),
                "operator_name": name,
                "operator_slug": slug,
                "period_start": inv.period_start.isoformat() if inv.period_start else None,
                "period_end": inv.period_end.isoformat() if inv.period_end else None,
                "amount_ghs": float(inv.amount_ghs),
                "status": inv.status,
                "issued_at": inv.issued_at.isoformat() if inv.issued_at else None,
                "due_at": inv.due_at.isoformat() if inv.due_at else None,
                "paid_at": inv.paid_at.isoformat() if inv.paid_at else None,
            }
            for inv, name, slug in rows
        ],
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
    }


# --- Payment transactions (platform owner only) ---
#
# Internal tool, not operator-facing: a way to flag a stray test/diagnostic
# transaction so it can be filtered out of an operator's own transaction
# history (#4 billing tab), replacing ad hoc SQL edits. No existing view
# showed individual transactions before this — operator_detail.html and
# operators.html only ever aggregate PaymentTransaction, they don't list rows.

_TERMINAL_TRANSACTION_STATUSES = {"success", "failed", "reversed"}


@router.get("/payment-transactions")
async def list_payment_transactions(
    page: int = 1,
    page_size: int = 50,
    operator_id: uuid.UUID | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    """Paginated list of every transaction, newest first. Same pattern as
    /billing/invoices above: clamped page_size, deterministic
    initiated_at DESC, id DESC ordering, a page past the end returns empty.

    Unlike the operator-facing /billing/transactions, there is deliberately no
    tenant predicate here — this view is platform-owner-scoped and spans every
    operator, so operator_id is an optional filter rather than a boundary.
    Diagnostic rows are shown too: this is the page you flag them from."""
    page = max(1, page)
    page_size = max(1, min(page_size, _INVOICES_PAGE_SIZE_MAX))

    # Built once, applied to both the count and the page query, so a total can
    # never disagree with the rows it is counting.
    filters = []
    if operator_id:
        filters.append(PaymentTransaction.isp_operator_id == operator_id)
    if start_date:
        filters.append(PaymentTransaction.initiated_at >= start_date)
    if end_date:
        filters.append(PaymentTransaction.initiated_at <= end_date)

    total_count = (
        await db.execute(
            select(func.count()).select_from(PaymentTransaction).where(*filters)
        )
    ).scalar() or 0
    total_pages = max(1, (total_count + page_size - 1) // page_size)

    rows = (
        await db.execute(
            select(PaymentTransaction, ISPOperator.name, ISPOperator.slug)
            .join(ISPOperator, PaymentTransaction.isp_operator_id == ISPOperator.id)
            .where(*filters)
            .order_by(PaymentTransaction.initiated_at.desc(), PaymentTransaction.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()

    return {
        "transactions": [
            {
                "id": str(tx.id),
                "operator_name": name,
                "operator_slug": slug,
                "amount_ghs": float(tx.amount_ghs),
                "payment_method": tx.payment_method,
                "provider": tx.provider,
                "status": tx.status,
                "is_diagnostic": bool(tx.is_diagnostic),
                "phone_number": tx.phone_number,
                "internal_reference": tx.internal_reference,
                "initiated_at": tx.initiated_at.isoformat() if tx.initiated_at else None,
            }
            for tx, name, slug in rows
        ],
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
    }


@router.patch("/payment-transactions/{transaction_id}/diagnostic")
async def set_transaction_diagnostic(
    transaction_id: uuid.UUID,
    body: TransactionDiagnosticUpdate,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    """Flag (or un-flag, for a mistaken mark) a transaction as an internal
    test artifact. Only terminal transactions — success/failed/reversed —
    can be marked; an in-flight one being flagged mid-flow doesn't mean
    anything, so this is a 409, not a silently-ignored write."""
    tx = (
        await db.execute(select(PaymentTransaction).where(PaymentTransaction.id == transaction_id))
    ).scalar_one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.status not in _TERMINAL_TRANSACTION_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Only terminal transactions ({', '.join(sorted(_TERMINAL_TRANSACTION_STATUSES))}) can be marked diagnostic.",
        )

    tx.is_diagnostic = body.is_diagnostic
    await db.commit()
    return {"id": str(tx.id), "is_diagnostic": bool(tx.is_diagnostic)}


# --- SMS usage records (platform owner only) ---
#
# The other half of a platform-gateway send. Deliberately its own list rather
# than a column on the transactions table above: sms_usage_records has no FK to
# payment_transactions, so the two can only be correlated by operator + a few
# seconds of timing, which is not a relationship this endpoint should pretend
# to have. Flagging one does not flag the other.


@router.get("/sms-usage-records")
async def list_sms_usage_records(
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    """Paginated list of every metered platform-gateway send, newest first.
    Same pagination contract as /billing/invoices and /payment-transactions."""
    page = max(1, page)
    page_size = max(1, min(page_size, _INVOICES_PAGE_SIZE_MAX))

    total_count = (
        await db.execute(select(func.count()).select_from(SMSUsageRecord))
    ).scalar() or 0
    total_pages = max(1, (total_count + page_size - 1) // page_size)

    rows = (
        await db.execute(
            select(SMSUsageRecord, ISPOperator.name, ISPOperator.slug)
            .join(ISPOperator, SMSUsageRecord.isp_operator_id == ISPOperator.id)
            .order_by(SMSUsageRecord.sent_at.desc(), SMSUsageRecord.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()

    return {
        "records": [
            {
                "id": str(rec.id),
                "operator_name": name,
                "operator_slug": slug,
                "segment_count": rec.segment_count,
                "rate_ghs_per_segment": str(rec.rate_ghs_per_segment),
                "amount_ghs": float(rec.amount_ghs),
                "provider_reference": rec.provider_reference,
                "sent_at": rec.sent_at.isoformat() if rec.sent_at else None,
                "is_billed": rec.invoice_line_item_id is not None,
                "is_diagnostic": bool(rec.is_diagnostic),
            }
            for rec, name, slug in rows
        ],
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
    }


@router.patch("/sms-usage-records/{record_id}/diagnostic")
async def set_sms_usage_diagnostic(
    record_id: uuid.UUID,
    body: TransactionDiagnosticUpdate,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    """Flag (or un-flag) a metered send as an internal test artifact, excluding
    it from roll_up_sms_usage.

    409 once the row has already been billed: the charge is on an issued
    invoice by then, and clearing the flag here would not remove it from that
    invoice — it would only make the record disagree with what the operator was
    actually charged. Unbilled rows are the only ones this can still change the
    outcome for."""
    rec = (
        await db.execute(select(SMSUsageRecord).where(SMSUsageRecord.id == record_id))
    ).scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="Usage record not found")
    if rec.invoice_line_item_id is not None:
        raise HTTPException(
            status_code=409,
            detail="This usage record has already been billed on an invoice and can no longer be flagged.",
        )

    rec.is_diagnostic = body.is_diagnostic
    await db.commit()
    return {"id": str(rec.id), "is_diagnostic": bool(rec.is_diagnostic)}


@router.put("/operators/{operator_id}/billing")
async def update_operator_billing(
    operator_id: uuid.UUID,
    body: PlatformOperatorBillingUpdate,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    """Set an operator's monthly fee and/or extend their trial."""
    operator = await db.get(ISPOperator, operator_id)
    if not operator:
        raise HTTPException(404, "Operator not found")
    if body.monthly_fee_ghs is None and body.extend_trial_days is None:
        raise HTTPException(400, "Provide monthly_fee_ghs and/or extend_trial_days")

    if body.monthly_fee_ghs is not None:
        operator.monthly_fee_ghs = body.monthly_fee_ghs
    if body.extend_trial_days is not None:
        # Previously a silent no-op when the operator had no trial to extend.
        if not operator.trial_ends_at:
            raise HTTPException(400, f"{operator.name} is not on a trial, so there is nothing to extend")
        operator.trial_ends_at = operator.trial_ends_at + timedelta(days=body.extend_trial_days)

    await db.commit()
    await db.refresh(operator)
    return {
        "message": "Updated",
        "operator_id": str(operator.id),
        "monthly_fee_ghs": float(operator.monthly_fee_ghs),
        "trial_ends_at": operator.trial_ends_at.isoformat() if operator.trial_ends_at else None,
    }


@router.put("/invoices/{invoice_id}/waive")
async def waive_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    invoice = await db.get(OperatorInvoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    if invoice.status == "paid":
        raise HTTPException(400, "Cannot waive a paid invoice")
    invoice.status = "waived"
    event = OperatorBillingEvent(
        isp_operator_id=invoice.isp_operator_id,
        event_type="waived",
        description=f"Invoice {invoice.invoice_number} waived by platform owner.",
        event_metadata={"invoice_number": invoice.invoice_number, "waived_by": str(owner.id)},
    )
    db.add(event)
    await db.commit()
    return {"message": "Invoice waived", "invoice_id": str(invoice.id)}


# --- Platform settings (platform owner only) ---

@router.get("/settings")
async def get_platform_settings(
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Return all platform-level settings as a flat key/value object.

    Keys: wg_server_endpoint, platform_app_url, webhook_base_url. Values come
    from the platform_settings table, falling back to the .env/config default.
    """
    from src.modules.platform.settings_service import get_all_settings
    return await get_all_settings(db)


@router.put("/settings")
async def update_platform_settings(
    updates: dict[str, str],
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Update one or more platform settings. Only known safe keys are allowed."""
    from src.modules.platform.settings_service import PLATFORM_SETTING_KEYS, get_all_settings, set_setting

    unknown = set(updates) - set(PLATFORM_SETTING_KEYS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown setting key(s): {', '.join(sorted(unknown))}",
        )
    for key, value in updates.items():
        await set_setting(db, key, str(value if value is not None else ""))
    await db.commit()
    return await get_all_settings(db)


# ---------------------------------------------------------------------------
# Platform analytics snapshot (platform owner only, strictly read-only)
# ---------------------------------------------------------------------------
# A point-in-time overview for /platform/analytics — no history, no trends.
# The MRR / revenue-collected figures are NOT recomputed here: that page reads
# them from /billing/summary directly, so the aggregate lives in exactly one
# place. This endpoint only adds the two things nothing else aggregates:
# operator counts by status and a platform-wide router online/offline tally.

# billing_status and status are different enums measuring different axes — an
# operator can be billing_status='active' while status='suspended' (a manual
# access cut). They are reported separately and never merged into one list.
_BILLING_STATUSES = ["trial", "active", "past_due", "cancelled"]


@router.get("/analytics/snapshot")
async def platform_analytics_snapshot(
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    # One grouped query for the billing-status breakdown, normalised so every
    # enum value is present (a status with no operators reads as 0, not absent).
    billing_rows = (
        await db.execute(
            select(ISPOperator.billing_status, func.count())
            .group_by(ISPOperator.billing_status)
        )
    ).all()
    by_billing_status = {name: 0 for name in _BILLING_STATUSES}
    for name, count in billing_rows:
        by_billing_status[name] = count

    # Access axis: how many operators are suspended regardless of billing state.
    access_suspended = (
        await db.execute(
            select(func.count()).select_from(ISPOperator).where(ISPOperator.status == "suspended")
        )
    ).scalar() or 0

    operators_total = (
        await db.execute(select(func.count()).select_from(ISPOperator))
    ).scalar() or 0

    # Router online/offline across every operator. The table is small (one
    # platform, a handful of routers) so a single scan and the shared
    # _is_online() predicate is cleaner than reimplementing it in SQL.
    routers = (await db.execute(select(Router))).scalars().all()
    routers_online = sum(1 for r in routers if router_is_online(r))

    return {
        "operators": {
            "total": operators_total,
            "by_billing_status": by_billing_status,
            "access_suspended": access_suspended,
        },
        "routers": {
            "total": len(routers),
            "online": routers_online,
            "offline": len(routers) - routers_online,
        },
    }


# --- Service health monitor (platform owner only, strictly read-only) ---

@router.get("/health/services")
async def platform_service_health(
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    """Per-service up/down/degraded status for the platform health monitor.

    Read-only by design: it probes reachability and app-level health endpoints
    the backend can already reach, and exposes no lifecycle controls. Restarting
    a service is Docker-daemon territory and is deliberately out of scope.

    Each probe opens its own connection, so a hung dependency can never
    invalidate the session this request is authenticated on.
    """
    from src.modules.platform.health_service import collect_service_health

    return await collect_service_health()


# --- Provider catalog (platform owner only) ---

def _catalog_row(entry: ProviderCatalogEntry) -> dict:
    return {
        "id": str(entry.id),
        "category": entry.category,
        "provider_key": entry.provider_key,
        "display_name": entry.display_name,
        "description": entry.description,
        "credential_schema": entry.credential_schema or {},
        "is_integrated": bool(entry.is_integrated),
        "is_available": bool(entry.is_available),
        "is_platform_provided": bool(entry.is_platform_provided),
        # String, not float: the rate is Numeric(10,4) and money must not go
        # through a binary float on the way to the browser.
        "platform_rate_per_segment": (
            str(entry.platform_rate_per_segment) if entry.platform_rate_per_segment is not None else None
        ),
        "sort_order": entry.sort_order,
    }


@router.get("/providers", response_model=dict[str, list[ProviderCatalogEntryResponse]])
async def list_provider_catalog(
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """The full provider catalog, grouped by category.

    Rows are read-only apart from the availability toggle and the per-message
    rate; there is deliberately no create or delete endpoint, since the catalog
    is defined by the migration/seed.
    """
    result = await db.execute(
        select(ProviderCatalogEntry).order_by(
            ProviderCatalogEntry.category,
            ProviderCatalogEntry.sort_order,
            ProviderCatalogEntry.display_name,
        )
    )
    entries = result.scalars().all()
    grouped: dict[str, list] = {"payment": [], "sms": []}
    for entry in entries:
        grouped.setdefault(entry.category, []).append(_catalog_row(entry))
    return grouped


@router.put("/providers/{entry_id}", response_model=ProviderCatalogEntryResponse)
async def update_provider_catalog_entry(
    entry_id: uuid.UUID,
    body: ProviderCatalogUpdate,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Toggle availability and/or set the platform per-message rate.

    Enabling a provider whose integration does not exist yet is a 409, not a
    silently-ignored write — the UI disables the control, but the server is what
    actually guarantees operators are never offered a dead provider.
    """
    entry = (
        await db.execute(select(ProviderCatalogEntry).where(ProviderCatalogEntry.id == entry_id))
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    if body.is_available is not None:
        if body.is_available and not entry.is_integrated:
            raise HTTPException(
                status_code=409,
                detail=f"{entry.display_name} is not yet integrated and cannot be made available to operators.",
            )
        entry.is_available = body.is_available

    if body.clear_platform_rate:
        entry.platform_rate_per_segment = None
    elif body.platform_rate_per_segment is not None:
        if not entry.is_platform_provided:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{entry.display_name} is not platform-provided; operators are billed by the "
                    "provider directly, so there is no platform rate to set."
                ),
            )
        entry.platform_rate_per_segment = body.platform_rate_per_segment

    entry.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(entry)
    return _catalog_row(entry)


# --- Platform payment credentials (platform owner only) ---
#
# The platform's own keys — how operator subscriptions are collected. Distinct
# from /payment-credentials, which is an operator's keys for selling vouchers.
#
# Storage and UI only, for now: initiate_invoice_payment and the platform-billing
# webhook still read settings directly. Switching them to this table is Phase 4,
# deliberately after the webhook gains amount/currency verification in Phase 3.

def _last4(value: str | None) -> str | None:
    return value[-4:] if value else None


def _validate_paystack_keys(public_key: str, secret_key: str) -> None:
    if not (public_key.startswith("pk_test_") or public_key.startswith("pk_live_")):
        raise HTTPException(status_code=400, detail="Paystack public key must start with pk_test_ or pk_live_")
    if not (secret_key.startswith("sk_test_") or secret_key.startswith("sk_live_")):
        raise HTTPException(status_code=400, detail="Paystack secret key must start with sk_test_ or sk_live_")



async def _credential_response(db: AsyncSession) -> PlatformPaymentCredentialResponse:
    """Masked view of whichever keys are actually in force.

    Only the last 4 characters ever leave this function — the raw values stay in
    the resolver.
    """
    from src.modules.platform import payment_credentials_service as creds_service

    # What is actually in force — the active row, or .env when there is none.
    keys = await creds_service.resolve_paystack_keys(db)
    # The stored row, if any, whether or not it is the one in force. A
    # deactivated row still needs reporting: "stored but switched off" is a
    # legitimate state (mid provider-switch, say), not an absence.
    row = await creds_service.get_credential(db)
    shown = creds_service.keys_from_credential(row) if row is not None else keys
    return PlatformPaymentCredentialResponse(
        provider=row.provider if row else "paystack",
        public_key_last4=_last4(shown.public_key),
        secret_key_last4=_last4(shown.secret_key),
        webhook_secret_last4=_last4(shown.webhook_secret),
        is_stored=row is not None,
        stored_updated_at=row.updated_at if row else None,
        is_active=bool(row.is_active) if row else False,
        is_configured=keys.is_configured,
        source=keys.source,
        last_validated_at=row.last_validated_at if row else None,
        last_validation_error=row.last_validation_error if row else None,
    )


@router.get("/payment-credentials", response_model=PlatformPaymentCredentialResponse)
async def get_platform_payment_credentials(
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Masked view of the platform's payment keys.

    `source` says whether the values come from the table ("db") or the
    PLATFORM_BILLING_PAYSTACK_* env vars ("env"), which is the read-through
    fallback while the table has no active row.
    """
    return await _credential_response(db)


@router.put("/payment-credentials", response_model=PlatformPaymentCredentialResponse)
async def update_platform_payment_credentials(
    body: PlatformPaymentCredentialUpdate,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Store the platform's payment keys, encrypted at rest.

    Writing a row makes the table authoritative — the .env fallback stops
    applying the moment an active row exists.
    """
    from src.modules.platform import payment_credentials_service as creds_service

    _validate_paystack_keys(body.public_key, body.secret_key)

    row = await creds_service.get_credential(db, body.provider.value)
    if row is None:
        row = PlatformPaymentCredential(provider=body.provider.value)
        db.add(row)

    row.public_key_encrypted = encrypt_secret(body.public_key)
    row.secret_key_encrypted = encrypt_secret(body.secret_key)
    row.webhook_secret_encrypted = encrypt_secret(body.webhook_secret) if body.webhook_secret else None
    row.is_active = body.is_active
    # The keys changed, so any previous validation result no longer describes them.
    row.last_validated_at = None
    row.last_validation_error = None
    row.updated_at = datetime.now(timezone.utc)

    await db.commit()
    return await _credential_response(db)


@router.post("/payment-credentials/test", response_model=PlatformPaymentCredentialResponse)
async def test_platform_payment_credentials(
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Check the resolved keys against Paystack.

    Tests whatever is actually in force, table or .env. The result is only
    persisted when it came from a stored row — there is nowhere to record a
    validation against .env values.
    """
    import httpx

    from src.modules.platform import payment_credentials_service as creds_service

    keys = await creds_service.resolve_paystack_keys(db)
    if not keys.is_configured:
        raise HTTPException(
            status_code=400,
            detail=(
                "Platform payment credentials are not configured — no active row in "
                "platform_payment_credentials and no PLATFORM_BILLING_PAYSTACK_* values in the environment."
            ),
        )

    row = await creds_service.get_active_credential(db)
    try:
        async with httpx.AsyncClient(timeout=15.0, base_url="https://api.paystack.co") as client:
            response = await client.get(
                "/transaction",
                params={"perPage": 1},
                headers={"Authorization": f"Bearer {keys.secret_key}"},
            )
            response.raise_for_status()
    except Exception as exc:
        if row is not None:
            row.last_validation_error = str(exc)
            await db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if row is not None:
        row.last_validated_at = datetime.now(timezone.utc)
        row.last_validation_error = None
        await db.commit()
    return await _credential_response(db)


# --- Platform SMS credentials (platform owner only) ---
#
# The platform's own Arkesel keys — how the platform-provided SMS gateway
# option (the operator-facing arkesel_platform marker in
# operator_sms_credentials.provider) is actually sent. Distinct from
# /sms-credentials, which is an operator's own bring-your-own keys.
#
# Single-blob shape (platform_sms_credentials.credentials_encrypted), not
# PlatformPaymentCredential's per-field columns — see PlatformSMSCredential's
# model docstring. No .env fallback: a platform-gateway send with nothing
# stored here is simply refused, not silently degraded.

async def _sms_credential_response(
    db: AsyncSession, test_detail: str | None = None
) -> PlatformSMSCredentialResponse:
    from src.modules.credentials.service import load_credentials, mask
    from src.modules.platform import platform_sms_credentials_service as creds_service

    row = await creds_service.get_credential(db)
    api_key_masked = None
    sender_id = None
    if row is not None:
        values = load_credentials(row)
        api_key_masked = mask(values.get("api_key"))
        sender_id = values.get("sender_id")
    return PlatformSMSCredentialResponse(
        provider=row.provider if row else creds_service.ARKESEL,
        api_key_masked=api_key_masked,
        sender_id=sender_id,
        is_stored=row is not None,
        stored_updated_at=row.updated_at if row else None,
        is_active=bool(row.is_active) if row else False,
        last_validated_at=row.last_validated_at if row else None,
        last_validation_error=row.last_validation_error if row else None,
        test_detail=test_detail,
    )


@router.get("/sms-credentials", response_model=PlatformSMSCredentialResponse)
async def get_platform_sms_credentials(
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Masked view of the platform's own Arkesel keys."""
    return await _sms_credential_response(db)


@router.put("/sms-credentials", response_model=PlatformSMSCredentialResponse)
async def update_platform_sms_credentials(
    body: PlatformSMSCredentialUpdate,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Store the platform's Arkesel keys, encrypted at rest as a single Fernet
    blob — the same shape an operator's own sms credentials use."""
    from src.modules.credentials.service import dump_credentials
    from src.modules.platform import platform_sms_credentials_service as creds_service

    if not body.api_key.strip() or not body.sender_id.strip():
        raise HTTPException(status_code=400, detail="api_key and sender_id are both required")

    row = await creds_service.get_credential(db)
    if row is None:
        row = PlatformSMSCredential(provider=creds_service.ARKESEL)
        db.add(row)

    row.credentials_encrypted = dump_credentials(
        {"api_key": body.api_key.strip(), "sender_id": body.sender_id.strip()}
    )
    row.is_active = body.is_active
    # The keys changed, so any previous validation result no longer describes them.
    row.last_validated_at = None
    row.last_validation_error = None
    row.updated_at = datetime.now(timezone.utc)

    await db.commit()
    return await _sms_credential_response(db)


@router.post("/sms-credentials/test", response_model=PlatformSMSCredentialResponse)
async def test_platform_sms_credentials(
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Auth-only check against Arkesel's balance endpoint — never sends a real
    message, same discipline as every other provider's Test Connection."""
    from src.modules.credentials.service import load_credentials
    from src.modules.platform import platform_sms_credentials_service as creds_service
    from src.modules.sms.providers.arkesel import ArkeselSMSProvider

    row = await creds_service.get_active_credential(db)
    if row is None:
        raise HTTPException(status_code=400, detail="No active platform SMS credential is stored.")

    values = load_credentials(row)
    provider = ArkeselSMSProvider(api_key=values["api_key"], sender_id=values["sender_id"])
    try:
        detail = await provider.verify_credentials()
    except Exception as exc:
        row.last_validation_error = str(exc)
        row.last_validated_at = None
        await db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    row.last_validated_at = datetime.now(timezone.utc)
    row.last_validation_error = None
    await db.commit()
    return await _sms_credential_response(
        db, test_detail=detail if isinstance(detail, str) and detail.strip() else None
    )


# --- Platform NOTIFICATION SMS credentials (platform owner only) ---
#
# The account used to text OPERATORS about their own account (trial expiry,
# invoices, suspension). Distinct from /sms-credentials above, which is the
# gateway operators resell to their customers — these are separate Arkesel
# accounts on purpose; see PlatformNotificationSMSCredential's model docstring.


async def _notification_sms_response(
    db: AsyncSession, test_detail: str | None = None
) -> PlatformNotificationSMSCredentialResponse:
    from src.modules.credentials.service import load_credentials, mask
    from src.modules.platform import notification_sms_credentials_service as creds_service

    row = await creds_service.get_credential(db)
    api_key_masked = None
    sender_id = None
    shares_gateway_account = True
    if row is not None:
        values = load_credentials(row)
        api_key_masked = mask(values.get("api_key"))
        sender_id = values.get("sender_id")
        shares_gateway_account = bool(values.get("shares_gateway_account", True))
    return PlatformNotificationSMSCredentialResponse(
        provider=row.provider if row else creds_service.ARKESEL,
        api_key_masked=api_key_masked,
        sender_id=sender_id,
        is_stored=row is not None,
        stored_updated_at=row.updated_at if row else None,
        is_active=bool(row.is_active) if row else False,
        last_validated_at=row.last_validated_at if row else None,
        last_validation_error=row.last_validation_error if row else None,
        shares_gateway_account=shares_gateway_account,
        test_detail=test_detail,
    )


@router.get("/notification-sms-credentials", response_model=PlatformNotificationSMSCredentialResponse)
async def get_platform_notification_sms_credentials(
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Masked view of the account used to notify operators."""
    return await _notification_sms_response(db)


@router.put("/notification-sms-credentials", response_model=PlatformNotificationSMSCredentialResponse)
async def update_platform_notification_sms_credentials(
    body: PlatformNotificationSMSCredentialUpdate,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Store the notification account's keys, encrypted at rest."""
    from src.modules.credentials.service import dump_credentials
    from src.modules.platform import notification_sms_credentials_service as creds_service

    if not body.api_key.strip() or not body.sender_id.strip():
        raise HTTPException(status_code=400, detail="api_key and sender_id are both required")

    row = await creds_service.get_credential(db)
    if row is None:
        row = PlatformNotificationSMSCredential(provider=creds_service.ARKESEL)
        db.add(row)

    row.credentials_encrypted = dump_credentials(
        {
            "api_key": body.api_key.strip(),
            "sender_id": body.sender_id.strip(),
            "shares_gateway_account": body.shares_gateway_account,
        }
    )
    row.is_active = body.is_active
    row.last_validated_at = None
    row.last_validation_error = None
    row.updated_at = datetime.now(timezone.utc)

    await db.commit()
    return await _notification_sms_response(db)


@router.post("/notification-sms-credentials/test", response_model=PlatformNotificationSMSCredentialResponse)
async def test_platform_notification_sms_credentials(
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Auth-only balance check — never sends a message, same as every other
    Test Connection in the portal."""
    from src.modules.credentials.service import load_credentials
    from src.modules.platform import notification_sms_credentials_service as creds_service
    from src.modules.sms.providers.arkesel import ArkeselSMSProvider

    row = await creds_service.get_active_credential(db)
    if row is None:
        raise HTTPException(status_code=400, detail="No active notification SMS credential is stored.")

    values = load_credentials(row)
    provider = ArkeselSMSProvider(api_key=values["api_key"], sender_id=values["sender_id"])
    try:
        detail = await provider.verify_credentials()
    except Exception as exc:
        row.last_validation_error = str(exc)
        row.last_validated_at = None
        await db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    row.last_validated_at = datetime.now(timezone.utc)
    row.last_validation_error = None
    await db.commit()
    return await _notification_sms_response(
        db, test_detail=detail if isinstance(detail, str) and detail.strip() else None
    )
