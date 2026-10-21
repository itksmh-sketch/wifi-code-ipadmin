import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import ISPOperator, Reseller, Site, Town
from src.middleware.auth import TenantContext, get_admin_tenant_context, require_recent_pin
from src.middleware.permissions import CONFIG, MONEY, VOUCHERS_GENERATE, require_perm
from src.middleware.tenant_refs import load_owned
from src.modules import audit
from src.modules.audit.fields import RESELLER, RESELLER_DEFAULTS
from src.modules.resellers import consignment
from src.utils.reseller_auth import hash_reseller_password

router = APIRouter(prefix="/admin", tags=["admin-resellers"])

# Body fields that name another row, and what each must be. A null clears the
# reference; anything else has to be the caller's own row (404 otherwise).
_RESELLER_REFS = {"town_id": (Town, "Town"), "site_id": (Site, "Site")}


async def _owned_refs(db: AsyncSession, payload: dict, refs: dict, operator_id) -> dict:
    """The referenced ids present in ``payload``, each checked to be the caller's."""
    checked = {}
    for field, (model, label) in refs.items():
        if field not in payload:
            continue
        value = payload[field]
        checked[field] = None if value in (None, "") else (await load_owned(db, model, value, operator_id, label=label)).id
    return checked


def _commission_from(payload: dict) -> tuple[str | None, object]:
    """(type, value) from a body that has ``commission_type``; both None when
    it is null (the operator default applies)."""
    if payload.get("commission_type") is None:
        return None, None
    return consignment.parse_commission(payload)


def _reseller_view(r: Reseller) -> dict:
    return {
        "id": str(r.id),
        "name": r.name,
        "email": r.email,
        "phone": r.phone,
        "role": r.role,
        "town_id": str(r.town_id) if r.town_id else None,
        "site_id": str(r.site_id) if r.site_id else None,
        "is_active": bool(r.is_active),
        # None: the operator's default (GET /admin/reseller-settings).
        "commission": consignment.commission_view(r.commission_type, r.commission_value),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "last_login_at": r.last_login_at.isoformat() if r.last_login_at else None,
    }


async def _operator(db: AsyncSession, operator_id) -> ISPOperator:
    return (await db.execute(select(ISPOperator).where(ISPOperator.id == operator_id))).scalar_one()


# ── operator defaults ─────────────────────────────────────────────────────


def _defaults_view(op: ISPOperator) -> dict:
    return {"commission": consignment.commission_view(op.reseller_commission_type, op.reseller_commission_value),
            "redeem_days": op.reseller_redeem_days}


@router.get("/reseller-settings", dependencies=[Depends(require_perm(CONFIG))])
async def admin_get_reseller_settings(db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    return _defaults_view(await _operator(db, tenant.isp_operator_id))


@router.put("/reseller-settings", dependencies=[Depends(audit.audited)])
async def admin_update_reseller_settings(payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_perm(CONFIG))):
    """The commission a reseller without its own earns, and the default
    sell-by in days (null: none). Applies to redemptions from now on; past
    ones keep the rate they were recorded at."""
    op = await _operator(db, tenant.isp_operator_id)
    before = RESELLER_DEFAULTS.snapshot(op)
    if "commission_type" in payload or "commission_value" in payload:
        op.reseller_commission_type, op.reseller_commission_value = consignment.parse_commission(payload)
    if "redeem_days" in payload:
        days = payload["redeem_days"]
        if days is not None and (not isinstance(days, int) or isinstance(days, bool)
                                 or not 1 <= days <= consignment.MAX_REDEEM_DAYS):
            raise HTTPException(status_code=400, detail=f"redeem_days must be null or 1 to {consignment.MAX_REDEEM_DAYS}")
        op.reseller_redeem_days = days
    await db.flush()
    await db.refresh(op)
    changes = RESELLER_DEFAULTS.diff(before, RESELLER_DEFAULTS.snapshot(op))
    if changes:
        audit.record(db, "reseller.defaults_update", target_type="operator", target_id=op.id,
                     summary="Changed the reseller defaults", changes=changes)
    await db.commit()
    return _defaults_view(op)


# ── resellers ─────────────────────────────────────────────────────────────


@router.get("/resellers", dependencies=[Depends(require_perm(CONFIG))])
async def admin_list_resellers(db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    resellers = (
        await db.execute(
            select(Reseller).where(Reseller.isp_operator_id == tenant.isp_operator_id).order_by(Reseller.created_at.desc())
        )
    ).scalars().all()
    totals = await consignment.summaries(db, tenant.isp_operator_id, [r.id for r in resellers])
    return [{**_reseller_view(r), "totals": totals[r.id]["totals"]} for r in resellers]


@router.post("/resellers", dependencies=[Depends(audit.audited)])
async def admin_create_reseller(payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_perm(CONFIG, active=True))):
    # Expected keys: name,email,phone,password,role,town_id,site_id; optional
    # commission_type + commission_value (absent: the operator default).
    email = (payload.get("email") or "").strip().lower()
    if not email or not payload.get("password") or not payload.get("name"):
        raise HTTPException(status_code=400, detail="Missing required fields")

    exists = (await db.execute(select(Reseller).where(Reseller.email == email))).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail="Email already exists")
    refs = await _owned_refs(db, payload, _RESELLER_REFS, tenant.isp_operator_id)
    commission_type, commission_value = _commission_from(payload)

    reseller = Reseller(
        isp_operator_id=tenant.isp_operator_id,
        name=payload["name"],
        email=email,
        phone=payload.get("phone"),
        password_hash=hash_reseller_password(payload["password"]),
        role=payload.get("role") or "reseller",
        town_id=refs.get("town_id"),
        site_id=refs.get("site_id"),
        is_active=bool(payload.get("is_active", True)),
        commission_type=commission_type,
        commission_value=commission_value,
    )
    db.add(reseller)
    await db.flush()
    await db.refresh(reseller)
    audit.record(db, "reseller.create", target_type="reseller", target_id=reseller.id,
                 summary=f"Added reseller {reseller.name} ({reseller.email})",
                 changes=RESELLER.diff(None, RESELLER.snapshot(reseller)))
    await db.commit()
    await db.refresh(reseller)
    return {"id": str(reseller.id)}


@router.get("/resellers/{reseller_id}", dependencies=[Depends(require_perm(CONFIG))])
async def admin_get_reseller(reseller_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    reseller = await consignment.owned_reseller(db, tenant.isp_operator_id, reseller_id)
    return {**_reseller_view(reseller), **await consignment.summary(db, tenant.isp_operator_id, reseller.id)}


@router.put("/resellers/{reseller_id}", dependencies=[Depends(audit.audited)])
async def admin_update_reseller(reseller_id: uuid.UUID, payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_perm(CONFIG))):
    reseller = (
        await db.execute(select(Reseller).where(Reseller.id == reseller_id, Reseller.isp_operator_id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if not reseller:
        raise HTTPException(status_code=404, detail="Reseller not found")
    before = RESELLER.snapshot(reseller)
    refs = await _owned_refs(db, payload, _RESELLER_REFS, tenant.isp_operator_id)

    if "email" in payload and payload["email"]:
        new_email = payload["email"].strip().lower()
        if new_email != reseller.email:
            exists = (await db.execute(select(Reseller).where(Reseller.email == new_email))).scalar_one_or_none()
            if exists:
                raise HTTPException(status_code=400, detail="Email already exists")
            reseller.email = new_email

    for field in ("name", "phone", "role", "is_active"):
        if field in payload:
            setattr(reseller, field, payload[field])
    for field, value in refs.items():
        setattr(reseller, field, value)
    # Applies to redemptions from now on; past ones keep the rate they earned.
    if "commission_type" in payload:
        reseller.commission_type, reseller.commission_value = _commission_from(payload)

    signed_out = False
    if payload.get("password"):
        reseller.password_hash = hash_reseller_password(payload["password"])
        signed_out = True
    if before["is_active"] and payload.get("is_active") is False:
        signed_out = True
    if signed_out:
        # Ends every session the reseller has, on every device.
        reseller.token_version = int(reseller.token_version or 0) + 1

    # Compared after a round trip, so "town_id" sent as text against a stored
    # UUID is not a change.
    await db.flush()
    await db.refresh(reseller)
    changes = RESELLER.diff(before, RESELLER.snapshot(reseller))
    if changes:
        audit.record(db, "reseller.update", target_type="reseller", target_id=reseller.id,
                     summary=f"Edited reseller {reseller.name}", changes=changes,
                     detail={"sessions_ended": True} if signed_out else None)
    await db.commit()
    return {"ok": True}


# ── allocations and codes ─────────────────────────────────────────────────


@router.get("/resellers/{reseller_id}/allocations", dependencies=[Depends(require_perm(CONFIG))])
async def admin_reseller_allocations(reseller_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    reseller = await consignment.owned_reseller(db, tenant.isp_operator_id, reseller_id)
    return await consignment.list_allocations(db, tenant.isp_operator_id, reseller.id)


@router.post("/resellers/{reseller_id}/allocations", dependencies=[Depends(audit.audited)])
async def admin_allocate_codes(
    reseller_id: uuid.UUID, payload: dict, db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_perm(VOUCHERS_GENERATE, active=True)),
):
    """Body: plan_id, quantity, optional redeem_by (ISO date-time; "" for none;
    absent for the operator default). Returns the new codes."""
    reseller = await consignment.owned_reseller(db, tenant.isp_operator_id, reseller_id)
    allocation, vouchers = await consignment.allocate(
        db, operator=await _operator(db, tenant.isp_operator_id), reseller=reseller, plan_id=payload.get("plan_id"),
        quantity=payload.get("quantity"), redeem_by=payload.get("redeem_by"), admin_id=tenant.user_id,
    )
    await db.commit()
    return {
        "allocation_id": str(allocation.id),
        "quantity": allocation.quantity,
        "unit_price_ghs": float(allocation.unit_price_ghs),
        "redeem_by": allocation.redeem_by.isoformat() if allocation.redeem_by else None,
        "codes": [v.code for v in vouchers],
    }


@router.get("/resellers/{reseller_id}/codes", dependencies=[Depends(require_perm(CONFIG))])
async def admin_reseller_codes(
    reseller_id: uuid.UUID, status: str | None = None, allocation_id: uuid.UUID | None = None, limit: int = 500,
    db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context),
):
    reseller = await consignment.owned_reseller(db, tenant.isp_operator_id, reseller_id)
    return await consignment.list_codes(db, tenant.isp_operator_id, reseller.id, state=status,
                                        allocation_id=allocation_id, limit=limit)


@router.post("/resellers/{reseller_id}/reclaim", dependencies=[Depends(audit.audited)])
async def admin_reclaim_codes(reseller_id: uuid.UUID, payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_perm(CONFIG))):
    """Body: voucher_ids (all must be reclaimable) or allocation_id (every
    remaining code of it), and a reason. Reclaimed codes stop working at once
    and for good."""
    reseller = await consignment.owned_reseller(db, tenant.isp_operator_id, reseller_id)
    count = await consignment.reclaim(
        db, operator_id=tenant.isp_operator_id, reseller=reseller, voucher_ids=payload.get("voucher_ids"),
        allocation_id=payload.get("allocation_id"), reason=payload.get("reason"), admin_id=tenant.user_id,
    )
    await db.commit()
    return {"reclaimed": count}


# ── the account ───────────────────────────────────────────────────────────


@router.get("/resellers/{reseller_id}/account", dependencies=[Depends(require_perm(CONFIG))])
async def admin_reseller_account(reseller_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    reseller = await consignment.owned_reseller(db, tenant.isp_operator_id, reseller_id)
    summary = await consignment.summary(db, tenant.isp_operator_id, reseller.id)
    ledger = await consignment.account(db, tenant.isp_operator_id, reseller.id, include_recorder=True)
    return {"totals": summary["totals"], **ledger}


@router.post("/resellers/{reseller_id}/payments",
             dependencies=[Depends(audit.audited), Depends(require_perm(MONEY)), Depends(require_recent_pin)])
async def admin_record_reseller_payment(reseller_id: uuid.UUID, payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    """Body: amount_ghs, optional received_on (YYYY-MM-DD, default today) and
    note. Recorded payments can't be edited or deleted; reverse one instead."""
    reseller = await consignment.owned_reseller(db, tenant.isp_operator_id, reseller_id)
    entry = await consignment.record_payment(
        db, operator_id=tenant.isp_operator_id, reseller=reseller, amount=payload.get("amount_ghs"),
        received_on=payload.get("received_on"), note=payload.get("note"), admin_id=tenant.user_id,
    )
    await db.commit()
    return {"id": str(entry.id), **(await consignment.summary(db, tenant.isp_operator_id, reseller.id))["totals"]}


@router.post("/resellers/{reseller_id}/payments/{entry_id}/reversal",
             dependencies=[Depends(audit.audited), Depends(require_perm(MONEY)), Depends(require_recent_pin)])
async def admin_reverse_reseller_payment(
    reseller_id: uuid.UUID, entry_id: uuid.UUID, payload: dict, db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    """Body: reason. Reverses the whole payment, once."""
    reseller = await consignment.owned_reseller(db, tenant.isp_operator_id, reseller_id)
    entry = await consignment.reverse_payment(
        db, operator_id=tenant.isp_operator_id, reseller=reseller, entry_id=entry_id, reason=payload.get("reason"),
        admin_id=tenant.user_id,
    )
    await db.commit()
    return {"id": str(entry.id), **(await consignment.summary(db, tenant.isp_operator_id, reseller.id))["totals"]}
