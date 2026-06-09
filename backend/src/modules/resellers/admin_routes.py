import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import CommissionRule, Plan, Reseller, ResellerWallet, ResellerVoucherAllocation, Voucher
from src.middleware.auth import TenantContext, get_admin_tenant_context, require_role
from src.modules.resellers.wallet_service import WalletService
from src.utils.reseller_auth import hash_reseller_password

router = APIRouter(prefix="/admin", tags=["admin-resellers"])
wallet_service = WalletService()


@router.get("/resellers")
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


@router.post("/resellers")
async def admin_create_reseller(payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_role("superadmin", "admin"))):
    # Expected keys: name,email,phone,password,role,town_id,site_id
    email = (payload.get("email") or "").strip().lower()
    if not email or not payload.get("password") or not payload.get("name"):
        raise HTTPException(status_code=400, detail="Missing required fields")

    exists = (await db.execute(select(Reseller).where(Reseller.email == email))).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail="Email already exists")

    reseller = Reseller(
        isp_operator_id=tenant.isp_operator_id,
        name=payload["name"],
        email=email,
        phone=payload.get("phone"),
        password_hash=hash_reseller_password(payload["password"]),
        role=payload.get("role") or "reseller",
        town_id=payload.get("town_id"),
        site_id=payload.get("site_id"),
        is_active=bool(payload.get("is_active", True)),
    )
    db.add(reseller)
    await db.flush()

    wallet = ResellerWallet(reseller_id=reseller.id, balance_ghs=Decimal("0.00"))
    db.add(wallet)
    await db.commit()
    await db.refresh(reseller)
    return {"id": str(reseller.id)}


@router.get("/resellers/{reseller_id}")
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


@router.put("/resellers/{reseller_id}")
async def admin_update_reseller(reseller_id: uuid.UUID, payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_role("superadmin", "admin"))):
    reseller = (
        await db.execute(select(Reseller).where(Reseller.id == reseller_id, Reseller.isp_operator_id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if not reseller:
        raise HTTPException(status_code=404, detail="Reseller not found")

    if "email" in payload and payload["email"]:
        new_email = payload["email"].strip().lower()
        if new_email != reseller.email:
            exists = (await db.execute(select(Reseller).where(Reseller.email == new_email))).scalar_one_or_none()
            if exists:
                raise HTTPException(status_code=400, detail="Email already exists")
            reseller.email = new_email

    for field in ("name", "phone", "role", "town_id", "site_id", "is_active"):
        if field in payload:
            setattr(reseller, field, payload[field])

    if payload.get("password"):
        reseller.password_hash = hash_reseller_password(payload["password"])

    await db.commit()
    return {"ok": True}


@router.put("/resellers/{reseller_id}/topup")
async def admin_reseller_topup(reseller_id: uuid.UUID, payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_role("superadmin", "admin"))):
    amount = payload.get("amount_ghs")
    if amount is None:
        raise HTTPException(status_code=400, detail="amount_ghs is required")
    description = payload.get("description") or "Admin topup"
    try:
        amount_dec = Decimal(str(amount))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid amount_ghs")

    tx_ctx = db.begin() if not db.in_transaction() else None
    if tx_ctx:
        await tx_ctx.__aenter__()
    try:
        reseller = (
            await db.execute(select(Reseller).where(Reseller.id == reseller_id, Reseller.isp_operator_id == tenant.isp_operator_id))
        ).scalar_one_or_none()
        if not reseller:
            raise HTTPException(status_code=404, detail="Reseller not found")
        await wallet_service.topup(
            db,
            reseller_id=reseller_id,
            amount_ghs=amount_dec,
            description=description,
            triggered_by="admin",
        )
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


@router.get("/resellers/{reseller_id}/vouchers")
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


@router.get("/commission-rules")
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


@router.post("/commission-rules")
async def admin_create_commission_rule(payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_role("superadmin", "admin"))):
    rule = CommissionRule(
        isp_operator_id=tenant.isp_operator_id,
        reseller_id=payload.get("reseller_id"),
        plan_id=payload.get("plan_id"),
        type=payload.get("type") or "percentage",
        value=payload.get("value") or 0,
        is_active=bool(payload.get("is_active", True)),
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return {"id": str(rule.id)}


@router.put("/commission-rules/{rule_id}")
async def admin_update_commission_rule(rule_id: uuid.UUID, payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_role("superadmin", "admin"))):
    rule = (
        await db.execute(select(CommissionRule).where(CommissionRule.id == rule_id, CommissionRule.isp_operator_id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    for field in ("reseller_id", "plan_id", "type", "value", "is_active"):
        if field in payload:
            setattr(rule, field, payload[field])
    await db.commit()
    return {"ok": True}


@router.delete("/commission-rules/{rule_id}")
async def admin_delete_commission_rule(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_role("superadmin", "admin"))):
    await db.execute(delete(CommissionRule).where(CommissionRule.id == rule_id, CommissionRule.isp_operator_id == tenant.isp_operator_id))
    await db.commit()
    return {"ok": True}
