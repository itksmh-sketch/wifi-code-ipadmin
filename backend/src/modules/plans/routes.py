from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete as sql_delete, func, or_, select
from src.db.base import get_db
from src.db.models import (
    CoAEvent,
    CommissionRule,
    PaymentTransaction,
    Plan,
    ResellerVoucherAllocation,
    Session,
    Site,
    Voucher,
)
from src.schemas import PlanCreate, PlanResponse, ErrorResponse
from src.middleware.auth import TenantContext, get_admin_tenant_context, require_active_operator
from src.modules.plans.dedup import DUPLICATE_PLAN_MESSAGE, SETTINGS_FIELDS, find_duplicate_plan
from src.utils.payload import parse_update, reject_unknown_fields
from fastapi import Body
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import List, Optional
import logging
import uuid

logger = logging.getLogger("plans")

router = APIRouter(prefix="/plans", tags=["plans"])


@router.get("", response_model=List[PlanResponse])
async def list_plans(db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    result = await db.execute(select(Plan).where(Plan.isp_operator_id == tenant.isp_operator_id).order_by(Plan.name))
    return result.scalars().all()


@router.post("", response_model=PlanResponse, status_code=201, responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
async def create_plan(body: PlanCreate, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_active_operator)):
    if body.type == "time" and body.duration_minutes is None:
        raise HTTPException(status_code=400, detail="duration_minutes is required for time-based plans")
    if body.type == "data" and body.data_limit_mb is None:
        raise HTTPException(status_code=400, detail="data_limit_mb is required for data-based plans")

    if body.site_id is not None:
        site = (
            await db.execute(select(Site).where(Site.id == body.site_id, Site.isp_operator_id == tenant.isp_operator_id))
        ).scalar_one_or_none()
        if not site:
            raise HTTPException(status_code=404, detail="Site not found")

    # Dedup on settings, not name: two plans that would behave identically for the
    # same operator and site are the same package under a different label.
    duplicate = await find_duplicate_plan(
        db,
        tenant.isp_operator_id,
        site_id=body.site_id,
        plan_type=body.type.value,
        duration_minutes=body.duration_minutes,
        data_limit_mb=body.data_limit_mb,
        download_speed_kbps=body.download_speed_kbps,
        upload_speed_kbps=body.upload_speed_kbps,
        price_ghs=body.price_ghs,
    )
    if duplicate:
        raise HTTPException(status_code=409, detail=f'{DUPLICATE_PLAN_MESSAGE} ("{duplicate.name}")')

    plan = Plan(
        isp_operator_id=tenant.isp_operator_id,
        site_id=body.site_id,
        name=body.name,
        type=body.type.value,
        duration_minutes=body.duration_minutes,
        data_limit_mb=body.data_limit_mb,
        download_speed_kbps=body.download_speed_kbps,
        upload_speed_kbps=body.upload_speed_kbps,
        price_ghs=body.price_ghs,
        is_active=body.is_active,
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return plan


@router.put("/{plan_id}", response_model=PlanResponse, responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
async def update_plan(
    plan_id: uuid.UUID,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_active_operator),
):
    """Kept for existing callers; identical to PATCH, including the allowlist.

    It used to accept every column, which let an edit rewrite the entitlement
    (type / duration_minutes / data_limit_mb) that already-issued vouchers are
    measured against — RADIUS reads the plan live and vouchers hold no snapshot.
    Use /activate and /deactivate to change availability.
    """
    return await _apply_plan_update(plan_id, payload, db, tenant)


# What a plan's entitlement means is fixed once vouchers exist: type, duration
# and data cap are read live by RADIUS on every login (vouchers store no
# snapshot), so editing them would silently rewrite what customers already
# bought. Price and speed are the commercial knobs that may move; changing the
# entitlement means creating a new plan and deactivating the old one.
PLAN_EDITABLE_FIELDS = {"name", "price_ghs", "download_speed_kbps", "upload_speed_kbps"}


class PlanProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, max_length=255)
    price_ghs: Optional[float] = Field(default=None, ge=0)
    download_speed_kbps: Optional[int] = Field(default=None, gt=0)
    upload_speed_kbps: Optional[int] = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("cannot be blank")
        return cleaned


@router.patch("/{plan_id}", response_model=PlanResponse, responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
async def patch_plan(
    plan_id: uuid.UUID,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_active_operator),
):
    """Edit a plan's name, price and speeds.

    type / duration_minutes / data_limit_mb are refused by name (400): they are
    the entitlement already-issued vouchers are measured against.
    """
    return await _apply_plan_update(plan_id, payload, db, tenant)


async def _apply_plan_update(plan_id: uuid.UUID, payload: dict, db: AsyncSession, tenant: TenantContext) -> Plan:
    reject_unknown_fields(payload, PLAN_EDITABLE_FIELDS)
    body = parse_update(PlanProfileUpdate, payload)

    result = await db.execute(select(Plan).where(Plan.id == plan_id, Plan.isp_operator_id == tenant.isp_operator_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    updates = {field: getattr(body, field) for field in PLAN_EDITABLE_FIELDS if field in payload}

    # Same duplicate guard the create/PUT paths use: an edit must not turn this
    # plan into a copy of another one.
    if SETTINGS_FIELDS & updates.keys():
        duplicate = await find_duplicate_plan(
            db,
            tenant.isp_operator_id,
            exclude_plan_id=plan.id,
            site_id=plan.site_id,
            plan_type=plan.type,
            duration_minutes=plan.duration_minutes,
            data_limit_mb=plan.data_limit_mb,
            download_speed_kbps=updates.get("download_speed_kbps", plan.download_speed_kbps),
            upload_speed_kbps=updates.get("upload_speed_kbps", plan.upload_speed_kbps),
            price_ghs=updates.get("price_ghs", plan.price_ghs),
        )
        if duplicate:
            raise HTTPException(status_code=409, detail=f'{DUPLICATE_PLAN_MESSAGE} ("{duplicate.name}")')

    for field, value in updates.items():
        setattr(plan, field, value)
    await db.commit()
    await db.refresh(plan)
    logger.info(
        "plan_updated plan_id=%s operator_id=%s fields=%s",
        plan_id, tenant.isp_operator_id, ",".join(sorted(updates)),
    )
    return plan


# A voucher is bearer credit: once it has been paid for, handed to a reseller,
# used, or disconnected, its row is financial/operational history and must not
# disappear. Deleting the plan it points at is only safe when every voucher it
# produced is still untouched.
VOUCHER_HISTORY_REASONS = {
    "payments": "have a payment record",
    "reseller_allocations": "were handed to a reseller",
    "sessions": "have been used (session recorded)",
    "coa_events": "have a disconnect record",
    "in_use_or_spent": "have been activated, disabled or used up",
}


async def _plan_history(db: AsyncSession, plan_id: uuid.UUID) -> dict[str, int]:
    """Counts of the things that make a plan undeletable. Empty dict == safe."""
    voucher_ids = select(Voucher.id).where(Voucher.plan_id == plan_id).scalar_subquery()

    async def count(stmt) -> int:
        return int((await db.execute(stmt)).scalar() or 0)

    found = {
        "payments": await count(
            select(func.count()).select_from(PaymentTransaction).where(PaymentTransaction.voucher_id.in_(voucher_ids))
        ),
        "reseller_allocations": await count(
            select(func.count()).select_from(ResellerVoucherAllocation)
            .where(ResellerVoucherAllocation.voucher_id.in_(voucher_ids))
        ),
        "sessions": await count(select(func.count()).select_from(Session).where(Session.voucher_id.in_(voucher_ids))),
        "coa_events": await count(select(func.count()).select_from(CoAEvent).where(CoAEvent.voucher_id.in_(voucher_ids))),
        # Belt and braces: a voucher that was used but whose session row was
        # never written (the Acct-Session-Id collision) still counts as history.
        "in_use_or_spent": await count(
            select(func.count()).select_from(Voucher).where(
                Voucher.plan_id == plan_id,
                or_(
                    Voucher.status != "unused",
                    Voucher.activated_at.is_not(None),
                    Voucher.data_used_mb > 0,
                ),
            )
        ),
        # payment_transactions.plan_id is its own FK: a charge can reference the
        # plan even when no voucher was ever issued (e.g. a failed payment).
        "plan_payments": await count(
            select(func.count()).select_from(PaymentTransaction).where(PaymentTransaction.plan_id == plan_id)
        ),
    }
    return {key: value for key, value in found.items() if value}


@router.delete("/{plan_id}", status_code=204, responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
async def delete_plan(plan_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    """Delete a plan and the untouched vouchers it produced.

    Refused (409, nothing written) as soon as any voucher of this plan carries
    history, or a payment references the plan directly. Deactivate instead:
    that stops new sales while leaving issued vouchers working.
    """
    result = await db.execute(select(Plan).where(Plan.id == plan_id, Plan.isp_operator_id == tenant.isp_operator_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    history = await _plan_history(db, plan_id)
    if history:
        parts = [
            f"{count} {'payment(s) reference this plan' if key == 'plan_payments' else 'voucher(s) ' + VOUCHER_HISTORY_REASONS[key]}"
            for key, count in history.items()
        ]
        raise HTTPException(
            status_code=409,
            detail=(
                "This plan has history and cannot be deleted: "
                + "; ".join(parts)
                + ". Deactivate it instead — that stops new sales and leaves issued vouchers working."
            ),
        )

    # Nothing to preserve: remove the untouched vouchers, this plan's commission
    # rules (forward-looking config — past commissions live on the wallet
    # transactions, which carry their own amounts), then the plan itself.
    vouchers_deleted = int(
        (await db.execute(select(func.count()).select_from(Voucher).where(Voucher.plan_id == plan_id))).scalar() or 0
    )
    rules_deleted = int(
        (await db.execute(select(func.count()).select_from(CommissionRule).where(CommissionRule.plan_id == plan_id))).scalar() or 0
    )
    await db.execute(sql_delete(Voucher).where(Voucher.plan_id == plan_id))
    await db.execute(sql_delete(CommissionRule).where(CommissionRule.plan_id == plan_id))
    await db.delete(plan)
    await db.commit()
    logger.info(
        "plan_deleted plan_id=%s operator_id=%s vouchers_deleted=%s commission_rules_deleted=%s",
        plan_id, tenant.isp_operator_id, vouchers_deleted, rules_deleted,
    )


@router.post("/{plan_id}/deactivate", response_model=PlanResponse, responses={404: {"model": ErrorResponse}})
async def deactivate_plan(plan_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    """Stop selling this plan. Vouchers already issued from it keep working."""
    return await _set_plan_active(db, plan_id, tenant, active=False)


@router.post("/{plan_id}/activate", response_model=PlanResponse, responses={404: {"model": ErrorResponse}})
async def activate_plan(plan_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_active_operator)):
    return await _set_plan_active(db, plan_id, tenant, active=True)


async def _set_plan_active(db: AsyncSession, plan_id: uuid.UUID, tenant: TenantContext, *, active: bool) -> Plan:
    result = await db.execute(select(Plan).where(Plan.id == plan_id, Plan.isp_operator_id == tenant.isp_operator_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    plan.is_active = active
    await db.commit()
    await db.refresh(plan)
    logger.info("plan_%s plan_id=%s operator_id=%s", "activated" if active else "deactivated", plan_id, tenant.isp_operator_id)
    return plan
