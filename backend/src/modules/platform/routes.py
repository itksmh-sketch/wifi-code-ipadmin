from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from decimal import Decimal
from typing import Optional
from src.db.models import (
    AdminUser,
    ISPOperator,
    OperatorInvoice,
    OperatorPaymentCredential,
    PaymentTransaction,
    PlatformOwner,
    Session,
    Voucher,
)
from src.middleware.auth import get_platform_owner_context
from src.middleware.rate_limit import enforce_rate_limit
from src.schemas import LoginRequest, PlatformAdminCreate, PlatformOperatorCreate, PlatformOperatorStatusUpdate, RefreshRequest, TokenResponse
from src.utils.auth import (
    create_platform_owner_access_token,
    create_platform_owner_refresh_token,
    hash_password,
    verify_password,
    verify_platform_owner_token,
)

router = APIRouter(prefix="/platform", tags=["platform"])

VALID_OPERATOR_STATUSES = {"pending", "approved", "suspended", "cancelled"}


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
    token_data = {"sub": str(owner.id), "role": "platform_owner", "email": owner.email}
    return TokenResponse(
        access_token=create_platform_owner_access_token(token_data),
        refresh_token=create_platform_owner_refresh_token(token_data),
    )


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
    token_data = {"sub": str(owner.id), "role": "platform_owner", "email": owner.email}
    return TokenResponse(
        access_token=create_platform_owner_access_token(token_data),
        refresh_token=create_platform_owner_refresh_token(token_data),
    )


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

    existing_admin = (
        await db.execute(select(AdminUser).where(AdminUser.email == body.initial_admin_email))
    ).scalar_one_or_none()
    if existing_admin:
        raise HTTPException(status_code=409, detail="Initial admin email already exists")

    operator = ISPOperator(
        name=body.name,
        slug=body.slug,
        contact_email=str(body.contact_email),
        contact_phone=body.contact_phone,
        status="approved",
        approved_at=datetime.now(timezone.utc),
        approved_by_platform_owner_id=owner.id,
        billing_status="active",
    )
    db.add(operator)
    await db.flush()

    admin = AdminUser(
        isp_operator_id=operator.id,
        email=str(body.initial_admin_email),
        password_hash=hash_password(body.initial_admin_password),
        role="superadmin",
        is_active=True,
    )
    db.add(admin)
    await db.commit()
    await db.refresh(operator)
    return await _operator_row(db, operator)


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

    operator.status = body.status
    operator.updated_at = datetime.now(timezone.utc)
    if body.status == "approved" and not operator.approved_at:
        operator.approved_at = datetime.now(timezone.utc)
        operator.approved_by_platform_owner_id = owner.id
    if body.status == "cancelled":
        operator.billing_status = "cancelled"
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
        }
        for admin in result.scalars().all()
    ]


@router.post("/operators/{operator_id}/admins", status_code=status.HTTP_201_CREATED)
async def create_operator_admin(
    operator_id: uuid.UUID,
    body: PlatformAdminCreate,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    if not await db.get(ISPOperator, operator_id):
        raise HTTPException(status_code=404, detail="Operator not found")
    existing = (await db.execute(select(AdminUser).where(AdminUser.email == body.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Admin email already exists")
    admin = AdminUser(
        isp_operator_id=operator_id,
        email=str(body.email),
        password_hash=hash_password(body.password),
        role=body.role.value,
        is_active=True,
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    return {
        "id": str(admin.id),
        "email": admin.email,
        "role": admin.role,
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
# Platform billing endpoints
# ---------------------------------------------------------------------------

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
    return {
        "total_active_operators": total_active,
        "operators_on_trial": on_trial,
        "operators_overdue": overdue,
        "monthly_recurring_revenue_ghs": float(mrr),
        "revenue_collected_this_month_ghs": float(collected),
    }


@router.get("/billing/operators")
async def platform_billing_operators(
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    operators = (await db.execute(select(ISPOperator).order_by(ISPOperator.name))).scalars().all()
    rows = []
    for op in operators:
        outstanding = (
            await db.execute(
                select(OperatorInvoice).where(
                    OperatorInvoice.isp_operator_id == op.id,
                    OperatorInvoice.status.in_(["issued", "overdue"]),
                )
            )
        ).scalar_one_or_none()
        last_paid = (
            await db.execute(
                select(OperatorInvoice).where(
                    OperatorInvoice.isp_operator_id == op.id,
                    OperatorInvoice.status == "paid",
                ).order_by(OperatorInvoice.paid_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        rows.append({
            "id": str(op.id),
            "name": op.name,
            "slug": op.slug,
            "billing_status": op.billing_status,
            "monthly_fee_ghs": float(op.monthly_fee_ghs),
            "last_paid_at": last_paid.paid_at.isoformat() if last_paid and last_paid.paid_at else None,
            "next_due_at": outstanding.due_at.isoformat() if outstanding and outstanding.due_at else None,
            "outstanding_amount_ghs": float(outstanding.amount_ghs) if outstanding else 0,
            "outstanding_invoice_number": outstanding.invoice_number if outstanding else None,
        })
    return rows


@router.put("/operators/{operator_id}/billing")
async def update_operator_billing(
    operator_id: uuid.UUID,
    monthly_fee_ghs: Optional[Decimal] = None,
    extend_trial_days: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    operator = await db.get(ISPOperator, operator_id)
    if not operator:
        raise HTTPException(404, "Operator not found")
    if monthly_fee_ghs is not None:
        operator.monthly_fee_ghs = monthly_fee_ghs
    if extend_trial_days is not None and operator.trial_ends_at:
        from datetime import timedelta
        operator.trial_ends_at = operator.trial_ends_at + timedelta(days=extend_trial_days)
    await db.commit()
    return {"message": "Updated", "operator_id": str(operator.id)}


@router.put("/invoices/{invoice_id}/waive")
async def waive_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    from src.db.models import OperatorBillingEvent
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
