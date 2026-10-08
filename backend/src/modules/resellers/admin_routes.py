import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import CommissionRule, Plan, Reseller, ResellerWallet, ResellerVoucherAllocation, Site, Town, Voucher
from src.middleware.auth import TenantContext, get_admin_tenant_context
from src.middleware.permissions import CONFIG, MONEY, require_perm
from src.middleware.tenant_refs import load_owned
from src.modules import audit
from src.modules.audit.fields import COMMISSION_RULE, RESELLER
from src.modules.resellers.wallet_service import WalletService
from src.utils.reseller_auth import hash_reseller_password

router = APIRouter(prefix="/admin", tags=["admin-resellers"])
wallet_service = WalletService()

# Body fields that name another row, and what each must be. A null clears the
# reference; anything else has to be the caller's own row (404 otherwise).
_RESELLER_REFS = {"town_id": (Town, "Town"), "site_id": (Site, "Site")}
_RULE_REFS = {"reseller_id": (Reseller, "Reseller"), "plan_id": (Plan, "Plan")}


async def _owned_refs(db: AsyncSession, payload: dict, refs: dict, operator_id) -> dict:
    """The referenced ids present in ``payload``, each checked to be the caller's."""
    checked = {}
    for field, (model, label) in refs.items():
        if field not in payload:
            continue
        value = payload[field]
        checked[field] = None if value in (None, "") else (await load_owned(db, model, value, operator_id, label=label)).id
    return checked


@router.get("/resellers", dependencies=[Depends(require_perm(CONFIG))])
async def admin_list_resellers(db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    res = await db.execute(
        select(Reseller, ResellerWallet)
        .join(ResellerWallet, ResellerWallet.reseller_id == Reseller.id, isouter=True)
        .where(Reseller.isp_operator_id == tenant.isp_operator_id)
        .order_by(Reseller.created_at.desc())
    )
    rows = res.all()
    out = []
    for r, w in rows:
        out.append(
            {
                "id": str(r.id),
                "name": r.name,
                "email": r.email,
                "phone": r.phone,
                "role": r.role,
                "town_id": str(r.town_id) if r.town_id else None,
                "site_id": str(r.site_id) if r.site_id else None,
                "is_active": bool(r.is_active),
                "balance_ghs": float(Decimal(str(w.balance_ghs))) if w else 0.0,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )
    return out


@router.post("/resellers", dependencies=[Depends(audit.audited)])
async def admin_create_reseller(payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_perm(CONFIG, active=True))):
    # Expected keys: name,email,phone,password,role,town_id,site_id
    email = (payload.get("email") or "").strip().lower()
    if not email or not payload.get("password") or not payload.get("name"):
        raise HTTPException(status_code=400, detail="Missing required fields")

    exists = (await db.execute(select(Reseller).where(Reseller.email == email))).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail="Email already exists")
    refs = await _owned_refs(db, payload, _RESELLER_REFS, tenant.isp_operator_id)

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
    )
    db.add(reseller)
    await db.flush()

    wallet = ResellerWallet(reseller_id=reseller.id, balance_ghs=Decimal("0.00"))
    db.add(wallet)
    await db.refresh(reseller)
    audit.record(db, "reseller.create", target_type="reseller", target_id=reseller.id,
                 summary=f"Added reseller {reseller.name} ({reseller.email})",
                 changes=RESELLER.diff(None, RESELLER.snapshot(reseller)))
    await db.commit()
    await db.refresh(reseller)
    return {"id": str(reseller.id)}


@router.get("/resellers/{reseller_id}", dependencies=[Depends(require_perm(CONFIG))])
async def admin_get_reseller(reseller_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    res = await db.execute(
        select(Reseller, ResellerWallet)
        .join(ResellerWallet, ResellerWallet.reseller_id == Reseller.id, isouter=True)
        .where(Reseller.id == reseller_id, Reseller.isp_operator_id == tenant.isp_operator_id)
    )
    row = res.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Reseller not found")
    r, w = row
    rules_res = await db.execute(
        select(CommissionRule)
        .where(CommissionRule.reseller_id == reseller_id, CommissionRule.isp_operator_id == tenant.isp_operator_id)
        .order_by(CommissionRule.created_at.desc())
    )
    rules = rules_res.scalars().all()
    return {
        "id": str(r.id),
        "name": r.name,
        "email": r.email,
        "phone": r.phone,
        "role": r.role,
        "town_id": str(r.town_id) if r.town_id else None,
        "site_id": str(r.site_id) if r.site_id else None,
        "is_active": bool(r.is_active),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "last_login_at": r.last_login_at.isoformat() if r.last_login_at else None,
        "wallet": {
            "balance_ghs": float(Decimal(str(w.balance_ghs))) if w else 0.0,
            "lifetime_topped_up_ghs": float(Decimal(str(w.lifetime_topped_up_ghs))) if w else 0.0,
            "lifetime_spent_ghs": float(Decimal(str(w.lifetime_spent_ghs))) if w else 0.0,
        },
        "commission_rules": [
            {
                "id": str(cr.id),
                "reseller_id": str(cr.reseller_id) if cr.reseller_id else None,
                "plan_id": str(cr.plan_id) if cr.plan_id else None,
                "type": cr.type,
                "value": float(Decimal(str(cr.value))),
                "is_active": bool(cr.is_active),
                "created_at": cr.created_at.isoformat() if cr.created_at else None,
            }
            for cr in rules
        ],
    }


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

    if payload.get("password"):
        reseller.password_hash = hash_reseller_password(payload["password"])

    # Compared after a round trip, so "town_id" sent as text against a stored
    # UUID is not a change.
    await db.flush()
    await db.refresh(reseller)
    changes = RESELLER.diff(before, RESELLER.snapshot(reseller))
    if changes:
        audit.record(db, "reseller.update", target_type="reseller", target_id=reseller.id,
                     summary=f"Edited reseller {reseller.name}", changes=changes)
    await db.commit()
    return {"ok": True}


@router.put("/resellers/{reseller_id}/topup", dependencies=[Depends(audit.audited)])
async def admin_reseller_topup(reseller_id: uuid.UUID, payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_perm(MONEY, active=True))):
    amount = payload.get("amount_ghs")
    if amount is None:
        raise HTTPException(status_code=400, detail="amount_ghs is required")
    description = payload.get("description") or "Admin topup"
    try:
        amount_dec = Decimal(str(amount))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid amount_ghs")
    # Checked here as well as in the wallet, whose ValueError surfaced as a 500.
    if not amount_dec.is_finite() or amount_dec <= 0:
        raise HTTPException(status_code=400, detail="amount_ghs must be greater than zero")

    tx_ctx = db.begin() if not db.in_transaction() else None
    if tx_ctx:
        await tx_ctx.__aenter__()
    try:
        reseller = (
            await db.execute(select(Reseller).where(Reseller.id == reseller_id, Reseller.isp_operator_id == tenant.isp_operator_id))
        ).scalar_one_or_none()
        if not reseller:
            raise HTTPException(status_code=404, detail="Reseller not found")
        wallet_tx = await wallet_service.topup(
            db,
            reseller_id=reseller_id,
            amount_ghs=amount_dec,
            description=description,
            triggered_by="admin",
        )
        # Credit an operator hands out: with the wallet change, or not at all.
        audit.record(db, "reseller.topup", target_type="reseller", target_id=reseller_id,
                     summary=f"Topped up reseller {reseller.name} by GHS {wallet_tx.amount_ghs}",
                     detail={"amount_ghs": wallet_tx.amount_ghs, "balance_after_ghs": wallet_tx.balance_after_ghs,
                             "description": description, "wallet_transaction_reference": wallet_tx.reference})
        if tx_ctx:
            await tx_ctx.__aexit__(None, None, None)
        else:
            await db.commit()
    except Exception:
        if tx_ctx:
            await tx_ctx.__aexit__(*__import__("sys").exc_info())
        else:
            await db.rollback()
        raise

    return {"ok": True}


@router.get("/resellers/{reseller_id}/vouchers", dependencies=[Depends(require_perm(CONFIG))])
async def admin_reseller_vouchers(reseller_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    res = await db.execute(
        select(ResellerVoucherAllocation, Voucher, Plan)
        .join(Voucher, Voucher.id == ResellerVoucherAllocation.voucher_id)
        .join(Plan, Plan.id == Voucher.plan_id)
        .where(ResellerVoucherAllocation.reseller_id == reseller_id, Voucher.isp_operator_id == tenant.isp_operator_id)
        .order_by(ResellerVoucherAllocation.allocated_at.desc())
        .limit(500)
    )
    rows = res.all()
    return [
        {
            "allocation_id": str(a.id),
            "voucher_id": str(v.id),
            "code": v.code,
            "plan_id": str(p.id),
            "plan_name": p.name,
            "plan_price_ghs": float(Decimal(str(p.price_ghs))),
            "purchase_price_ghs": float(Decimal(str(a.purchase_price_ghs))),
            "allocated_at": a.allocated_at.isoformat() if a.allocated_at else None,
            "sold_at": a.sold_at.isoformat() if a.sold_at else None,
            "sold_to_phone": a.sold_to_phone,
            "voucher_status": v.status,
        }
        for a, v, p in rows
    ]


@router.get("/commission-rules", dependencies=[Depends(require_perm(CONFIG))])
async def admin_list_commission_rules(
    reseller_id: uuid.UUID | None = None,
    plan_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    filters = [CommissionRule.is_active.in_([True, False]), CommissionRule.isp_operator_id == tenant.isp_operator_id]
    if reseller_id is not None:
        filters.append(CommissionRule.reseller_id == reseller_id)
    if plan_id is not None:
        filters.append(CommissionRule.plan_id == plan_id)
    res = await db.execute(select(CommissionRule).where(and_(*filters)).order_by(CommissionRule.created_at.desc()))
    rules = res.scalars().all()
    return [
        {
            "id": str(r.id),
            "reseller_id": str(r.reseller_id) if r.reseller_id else None,
            "plan_id": str(r.plan_id) if r.plan_id else None,
            "type": r.type,
            "value": float(Decimal(str(r.value))),
            "is_active": bool(r.is_active),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rules
    ]


@router.post("/commission-rules", dependencies=[Depends(audit.audited)])
async def admin_create_commission_rule(payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_perm(CONFIG))):
    refs = await _owned_refs(db, payload, _RULE_REFS, tenant.isp_operator_id)
    rule = CommissionRule(
        isp_operator_id=tenant.isp_operator_id,
        reseller_id=refs.get("reseller_id"),
        plan_id=refs.get("plan_id"),
        type=payload.get("type") or "percentage",
        value=payload.get("value") or 0,
        is_active=bool(payload.get("is_active", True)),
    )
    db.add(rule)
    await db.flush()
    await db.refresh(rule)
    audit.record(db, "commission_rule.create", target_type="commission_rule", target_id=rule.id,
                 summary=f"Added a {rule.type} commission rule ({rule.value})",
                 changes=COMMISSION_RULE.diff(None, COMMISSION_RULE.snapshot(rule)))
    await db.commit()
    return {"id": str(rule.id)}


@router.put("/commission-rules/{rule_id}", dependencies=[Depends(audit.audited)])
async def admin_update_commission_rule(rule_id: uuid.UUID, payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_perm(CONFIG))):
    rule = (
        await db.execute(select(CommissionRule).where(CommissionRule.id == rule_id, CommissionRule.isp_operator_id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    before = COMMISSION_RULE.snapshot(rule)
    refs = await _owned_refs(db, payload, _RULE_REFS, tenant.isp_operator_id)
    for field in ("type", "value", "is_active"):
        if field in payload:
            setattr(rule, field, payload[field])
    for field, value in refs.items():
        setattr(rule, field, value)
    await db.flush()
    await db.refresh(rule)
    changes = COMMISSION_RULE.diff(before, COMMISSION_RULE.snapshot(rule))
    if changes:
        audit.record(db, "commission_rule.update", target_type="commission_rule", target_id=rule.id,
                     summary="Edited a commission rule", changes=changes)
    await db.commit()
    return {"ok": True}


@router.delete("/commission-rules/{rule_id}", dependencies=[Depends(audit.audited)])
async def admin_delete_commission_rule(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_perm(CONFIG))):
    rule = (
        await db.execute(select(CommissionRule).where(CommissionRule.id == rule_id, CommissionRule.isp_operator_id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    # Unchanged contract: deleting a rule that isn't there is still {"ok": true}, with no row.
    if rule is not None:
        audit.record(db, "commission_rule.delete", target_type="commission_rule", target_id=rule.id,
                     summary=f"Deleted a {rule.type} commission rule ({rule.value})",
                     changes=COMMISSION_RULE.diff(COMMISSION_RULE.snapshot(rule), None))
    await db.execute(delete(CommissionRule).where(CommissionRule.id == rule_id, CommissionRule.isp_operator_id == tenant.isp_operator_id))
    await db.commit()
    return {"ok": True}
