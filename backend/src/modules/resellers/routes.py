import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import Plan, Reseller, ResellerWallet, ResellerVoucherAllocation, Site, Voucher
from src.middleware.reseller_auth import get_current_reseller, update_reseller_last_login
from src.middleware.rate_limit import enforce_rate_limit
from src.modules.resellers.wallet_service import InsufficientFundsError, WalletService
from src.modules.vouchers.engine import generate_voucher_code, generate_voucher_password, generate_voucher_username
from src.modules.payments.service import PaymentService
from src.schemas import (
    ErrorResponse,
    LoginRequest,
    RefreshRequest,
    ResellerMarkSoldRequest,
    ResellerVoucherPurchaseRequest,
    ResellerWalletTransactionResponse,
    TokenResponse,
)
from src.utils.reseller_auth import (
    create_reseller_access_token,
    create_reseller_refresh_token,
    verify_reseller_password,
    verify_reseller_token,
)

router = APIRouter(prefix="/reseller", tags=["reseller"])
wallet_service = WalletService()


@router.post("/auth/login", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
async def reseller_login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "reseller:login", limit=10, window_seconds=60)

    result = await db.execute(select(Reseller).where(Reseller.email == body.email, Reseller.is_active == True))  # noqa: E712
    reseller = result.scalar_one_or_none()
    if not reseller or not verify_reseller_password(body.password, reseller.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    await update_reseller_last_login(reseller.id, db)

    token_data = {
        "sub": str(reseller.id),
        "role": reseller.role,
        "email": reseller.email,
        "isp_operator_id": str(reseller.isp_operator_id),
        "town_id": str(reseller.town_id) if reseller.town_id else None,
        "site_id": str(reseller.site_id) if reseller.site_id else None,
    }
    return TokenResponse(
        access_token=create_reseller_access_token(token_data),
        refresh_token=create_reseller_refresh_token(token_data),
    )


@router.post("/auth/refresh", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
async def reseller_refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    payload = verify_reseller_token(body.refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    reseller_id = payload.get("sub")
    result = await db.execute(
        select(Reseller).where(
            Reseller.id == reseller_id,
            Reseller.isp_operator_id == payload.get("isp_operator_id"),
            Reseller.is_active == True,
        )
    )  # noqa: E712
    reseller = result.scalar_one_or_none()
    if not reseller:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Reseller not found or inactive")

    token_data = {
        "sub": str(reseller.id),
        "role": reseller.role,
        "email": reseller.email,
        "isp_operator_id": str(reseller.isp_operator_id),
        "town_id": str(reseller.town_id) if reseller.town_id else None,
        "site_id": str(reseller.site_id) if reseller.site_id else None,
    }
    return TokenResponse(
        access_token=create_reseller_access_token(token_data),
        refresh_token=create_reseller_refresh_token(token_data),
    )


@router.get("/me")
async def reseller_me(reseller: Reseller = Depends(get_current_reseller), db: AsyncSession = Depends(get_db)):
    wallet_res = await db.execute(select(ResellerWallet).where(ResellerWallet.reseller_id == reseller.id))
    wallet = wallet_res.scalar_one_or_none()
    balance = Decimal(str(wallet.balance_ghs)) if wallet else Decimal("0.00")
    return {
        "id": str(reseller.id),
        "name": reseller.name,
        "email": reseller.email,
        "phone": reseller.phone,
        "role": reseller.role,
        "town_id": str(reseller.town_id) if reseller.town_id else None,
        "site_id": str(reseller.site_id) if reseller.site_id else None,
        "is_active": bool(reseller.is_active),
        "wallet_balance_ghs": float(balance),
    }


@router.get("/wallet")
async def reseller_wallet(reseller: Reseller = Depends(get_current_reseller), db: AsyncSession = Depends(get_db)):
    wallet_res = await db.execute(select(ResellerWallet).where(ResellerWallet.reseller_id == reseller.id))
    wallet = wallet_res.scalar_one_or_none()
    if not wallet:
        return {"wallet_id": None, "balance_ghs": 0.0, "lifetime_topped_up_ghs": 0.0, "lifetime_spent_ghs": 0.0}
    return {
        "wallet_id": str(wallet.id),
        "balance_ghs": float(Decimal(str(wallet.balance_ghs))),
        "lifetime_topped_up_ghs": float(Decimal(str(wallet.lifetime_topped_up_ghs))),
        "lifetime_spent_ghs": float(Decimal(str(wallet.lifetime_spent_ghs))),
    }


@router.get("/wallet/transactions", response_model=list[ResellerWalletTransactionResponse])
async def reseller_wallet_transactions(
    limit: int = 20,
    offset: int = 0,
    wallet_id: uuid.UUID | None = None,
    reseller: Reseller = Depends(get_current_reseller),
    db: AsyncSession = Depends(get_db),
):
    if wallet_id is not None:
        wallet_res = await db.execute(select(ResellerWallet.id).where(ResellerWallet.reseller_id == reseller.id))
        own_wallet_id = wallet_res.scalar_one_or_none()
        if own_wallet_id is None:
            return []
        if wallet_id != own_wallet_id:
            # Ignore foreign wallet ids and keep reseller-scoped behavior.
            wallet_id = own_wallet_id
    txs = await wallet_service.get_transactions(db, reseller.id, limit=limit, offset=offset)
    return txs


@router.get("/plans")
async def reseller_plans(reseller: Reseller = Depends(get_current_reseller), db: AsyncSession = Depends(get_db)):
    # Plans can be global (site_id NULL), site-specific, or scoped via town through sites.
    filters = [Plan.is_active == True, Plan.isp_operator_id == reseller.isp_operator_id]  # noqa: E712
    if reseller.site_id:
        filters.append(or_(Plan.site_id.is_(None), Plan.site_id == reseller.site_id))
    elif reseller.town_id:
        town_site_ids = select(Site.id).where(Site.town_id == reseller.town_id, Site.isp_operator_id == reseller.isp_operator_id)
        filters.append(or_(Plan.site_id.is_(None), Plan.site_id.in_(town_site_ids)))
    else:
        filters.append(Plan.site_id.is_(None))

    res = await db.execute(select(Plan).where(and_(*filters)).order_by(Plan.price_ghs.asc()))
    plans = res.scalars().all()
    out = []
    for p in plans:
        commission = await wallet_service.calculate_commission(db, reseller.id, p.id)
        unit_cost = (Decimal(str(p.price_ghs)) - commission).quantize(Decimal("0.01"))
        if unit_cost < 0:
            unit_cost = Decimal("0.00")
        out.append(
            {
                "id": str(p.id),
                "site_id": str(p.site_id) if p.site_id else None,
                "name": p.name,
                "type": p.type,
                "duration_minutes": p.duration_minutes,
                "data_limit_mb": p.data_limit_mb,
                "download_speed_kbps": p.download_speed_kbps,
                "upload_speed_kbps": p.upload_speed_kbps,
                "price_ghs": float(Decimal(str(p.price_ghs))),
                "commission_ghs": float(commission),
                "unit_cost_ghs": float(unit_cost),
            }
        )
    return out


@router.post("/vouchers/purchase")
async def reseller_purchase_vouchers(
    body: ResellerVoucherPurchaseRequest,
    reseller: Reseller = Depends(get_current_reseller),
    db: AsyncSession = Depends(get_db),
):
    if body.quantity < 1 or body.quantity > 1000:
        raise HTTPException(status_code=400, detail="Quantity must be between 1 and 1000")

    tx_ctx = db.begin() if not db.in_transaction() else None
    if tx_ctx:
        await tx_ctx.__aenter__()
    try:
        plan_res = await db.execute(
            select(Plan).where(Plan.id == body.plan_id, Plan.isp_operator_id == reseller.isp_operator_id, Plan.is_active == True)
        )  # noqa: E712
        plan = plan_res.scalar_one_or_none()
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found or inactive")

        # Enforce reseller site restriction (if any)
        if reseller.site_id and plan.site_id not in (None, reseller.site_id):
            raise HTTPException(status_code=403, detail="Plan not available for reseller")
        if not reseller.site_id and reseller.town_id and plan.site_id is not None:
            plan_site_res = await db.execute(select(Site.town_id).where(Site.id == plan.site_id, Site.isp_operator_id == reseller.isp_operator_id))
            plan_town_id = plan_site_res.scalar_one_or_none()
            if plan_town_id != reseller.town_id:
                raise HTTPException(status_code=403, detail="Plan not available for reseller")
        if not reseller.site_id and not reseller.town_id and plan.site_id is not None:
            raise HTTPException(status_code=403, detail="Plan not available for reseller")

        commission = await wallet_service.calculate_commission(db, reseller.id, plan.id)
        unit_cost = (Decimal(str(plan.price_ghs)) - commission).quantize(Decimal("0.01"))
        if unit_cost < 0:
            unit_cost = Decimal("0.00")
        total_cost = (unit_cost * Decimal(body.quantity)).quantize(Decimal("0.01"))

        # Lock wallet and check funds once upfront (still each purchase writes its own wallet tx row).
        balance = await wallet_service.get_balance(db, reseller.id)
        if balance < total_cost:
            raise HTTPException(status_code=400, detail="INSUFFICIENT_FUNDS")

        batch_id = f"RES-{str(reseller.id)[:8]}-{str(uuid.uuid4())[:8]}"
        generated_codes: list[str] = []

        for _ in range(body.quantity):
            expires_at = None
            if plan.duration_minutes and plan.type in ("time", "hybrid"):
                expires_at = datetime.now(timezone.utc) + timedelta(minutes=plan.duration_minutes)

            v = Voucher(
                isp_operator_id=reseller.isp_operator_id,
                plan_id=plan.id,
                site_id=plan.site_id if plan.site_id else reseller.site_id,
                code=generate_voucher_code(),
                username=generate_voucher_username(),
                password=generate_voucher_password(),
                status="unused",
                device_policy="single",
                max_devices=1,
                expires_at=expires_at,
                batch_id=batch_id,
            )
            db.add(v)
            await db.flush()

            try:
                await wallet_service.purchase(db, reseller_id=reseller.id, voucher_id=v.id, plan_id=plan.id)
            except InsufficientFundsError:
                raise HTTPException(status_code=400, detail="INSUFFICIENT_FUNDS")

            generated_codes.append(v.code)

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

    return {
        "batch_id": batch_id,
        "quantity": body.quantity,
        "unit_cost_ghs": float(unit_cost),
        "total_cost_ghs": float(total_cost),
        "voucher_codes": generated_codes,
    }


@router.get("/vouchers")
async def reseller_vouchers(reseller: Reseller = Depends(get_current_reseller), db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(ResellerVoucherAllocation, Voucher, Plan)
        .join(Voucher, Voucher.id == ResellerVoucherAllocation.voucher_id)
        .join(Plan, Plan.id == Voucher.plan_id)
        .where(ResellerVoucherAllocation.reseller_id == reseller.id)
        .order_by(ResellerVoucherAllocation.allocated_at.desc())
    )
    rows = res.all()
    out = []
    for alloc, v, p in rows:
        out.append(
            {
                "allocation_id": str(alloc.id),
                "voucher_id": str(v.id),
                "code": v.code,
                "plan": {"id": str(p.id), "name": p.name, "price_ghs": float(Decimal(str(p.price_ghs)))},
                "allocated_at": alloc.allocated_at.isoformat() if alloc.allocated_at else None,
                "sold_at": alloc.sold_at.isoformat() if alloc.sold_at else None,
                "sold_to_phone": alloc.sold_to_phone,
                "purchase_price_ghs": float(Decimal(str(alloc.purchase_price_ghs))),
                "status": v.status,
                "expires_at": v.expires_at.isoformat() if v.expires_at else None,
            }
        )
    return out


@router.get("/vouchers/{voucher_id}")
async def reseller_voucher_detail(
    voucher_id: uuid.UUID,
    reseller: Reseller = Depends(get_current_reseller),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(ResellerVoucherAllocation, Voucher, Plan)
        .join(Voucher, Voucher.id == ResellerVoucherAllocation.voucher_id)
        .join(Plan, Plan.id == Voucher.plan_id)
        .where(ResellerVoucherAllocation.reseller_id == reseller.id, ResellerVoucherAllocation.voucher_id == voucher_id)
    )
    row = res.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Voucher not found")
    alloc, v, p = row
    return {
        "allocation_id": str(alloc.id),
        "voucher_id": str(v.id),
        "code": v.code,
        "plan": {"id": str(p.id), "name": p.name, "price_ghs": float(Decimal(str(p.price_ghs)))},
        "allocated_at": alloc.allocated_at.isoformat() if alloc.allocated_at else None,
        "sold_at": alloc.sold_at.isoformat() if alloc.sold_at else None,
        "sold_to_phone": alloc.sold_to_phone,
        "purchase_price_ghs": float(Decimal(str(alloc.purchase_price_ghs))),
        "status": v.status,
        "expires_at": v.expires_at.isoformat() if v.expires_at else None,
    }


@router.put("/vouchers/{allocation_id}/mark-sold")
async def reseller_mark_sold(
    allocation_id: uuid.UUID,
    body: ResellerMarkSoldRequest,
    reseller: Reseller = Depends(get_current_reseller),
    db: AsyncSession = Depends(get_db),
):
    tx_ctx = db.begin() if not db.in_transaction() else None
    if tx_ctx:
        await tx_ctx.__aenter__()
    try:
        res = await db.execute(
            select(ResellerVoucherAllocation)
            .where(ResellerVoucherAllocation.id == allocation_id, ResellerVoucherAllocation.reseller_id == reseller.id)
            .with_for_update()
        )
        alloc = res.scalar_one_or_none()
        if not alloc:
            raise HTTPException(status_code=404, detail="Allocation not found")
        if alloc.sold_at is not None:
            return {"ok": True}

        alloc.sold_at = datetime.now(timezone.utc)
        alloc.sold_to_phone = PaymentService.normalize_phone(body.sold_to_phone)

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


@router.get("/sales/summary")
async def reseller_sales_summary(reseller: Reseller = Depends(get_current_reseller), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    res = await db.execute(
        select(func.count(), func.coalesce(func.sum(Plan.price_ghs), 0), func.coalesce(func.sum(Plan.price_ghs - ResellerVoucherAllocation.purchase_price_ghs), 0))
        .select_from(ResellerVoucherAllocation)
        .join(Voucher, Voucher.id == ResellerVoucherAllocation.voucher_id)
        .join(Plan, Plan.id == Voucher.plan_id)
        .where(
            ResellerVoucherAllocation.reseller_id == reseller.id,
            ResellerVoucherAllocation.sold_at.is_not(None),
            ResellerVoucherAllocation.sold_at >= start,
        )
    )
    count, total_value, commission = res.one()
    return {
        "month": start.strftime("%Y-%m"),
        "sold_count": int(count or 0),
        "total_value_ghs": float(Decimal(str(total_value))),
        "commission_earned_ghs": float(Decimal(str(commission))),
    }


@router.get("/sales/daily")
async def reseller_sales_daily(
    days: int = 30,
    reseller: Reseller = Depends(get_current_reseller),
    db: AsyncSession = Depends(get_db),
):
    days = max(1, min(days, 90))
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    res = await db.execute(
        select(func.date_trunc("day", ResellerVoucherAllocation.sold_at).label("day"), func.count().label("count"))
        .where(
            ResellerVoucherAllocation.reseller_id == reseller.id,
            ResellerVoucherAllocation.sold_at.is_not(None),
            ResellerVoucherAllocation.sold_at >= start,
        )
        .group_by("day")
        .order_by("day")
    )
    counts = {row.day.date().isoformat(): int(row.count) for row in res.all()}

    series = []
    for i in range(days):
        d = (start + timedelta(days=i)).date().isoformat()
        series.append({"date": d, "count": counts.get(d, 0)})
    return series
