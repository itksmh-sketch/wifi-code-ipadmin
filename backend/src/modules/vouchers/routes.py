from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from src.db.base import get_db
from src.db.models import Voucher
from src.schemas import (
    VoucherGenerate, VoucherResponse, VoucherListResponse,
    VoucherUpdate, ErrorResponse, VoucherStatus,
)
from src.middleware.auth import TenantContext, get_admin_tenant_context, require_active_operator
from src.modules.vouchers.engine import disable_voucher_with_disconnect, generate_vouchers, restore_voucher_status
from typing import List, Optional
import uuid

router = APIRouter(prefix="/vouchers", tags=["vouchers"])


@router.get("", response_model=VoucherListResponse)
async def list_vouchers(
    status_filter: Optional[VoucherStatus] = Query(None, alias="status"),
    site_id: Optional[uuid.UUID] = Query(None),
    plan_id: Optional[uuid.UUID] = Query(None),
    batch_id: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    query = select(Voucher).where(Voucher.isp_operator_id == tenant.isp_operator_id)
    count_query = select(func.count()).select_from(Voucher).where(Voucher.isp_operator_id == tenant.isp_operator_id)

    if status_filter:
        query = query.where(Voucher.status == status_filter.value)
        count_query = count_query.where(Voucher.status == status_filter.value)
    if site_id:
        query = query.where(Voucher.site_id == site_id)
        count_query = count_query.where(Voucher.site_id == site_id)
    if plan_id:
        query = query.where(Voucher.plan_id == plan_id)
        count_query = count_query.where(Voucher.plan_id == plan_id)
    if batch_id:
        query = query.where(Voucher.batch_id == batch_id)
        count_query = count_query.where(Voucher.batch_id == batch_id)

    query = query.order_by(Voucher.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    vouchers = result.scalars().all()

    count_result = await db.execute(count_query)
    total = count_result.scalar()

    return VoucherListResponse(vouchers=vouchers, total=total)


@router.post("/generate", response_model=List[VoucherResponse], status_code=201, responses={400: {"model": ErrorResponse}})
async def generate_voucher_batch(
    body: VoucherGenerate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_active_operator),
):
    if body.quantity < 1 or body.quantity > 500:
        raise HTTPException(status_code=400, detail="Quantity must be between 1 and 500")

    try:
        vouchers, batch_id = await generate_vouchers(db, body, tenant.isp_operator_id)
        from src.modules.onboarding import mark_checklist
        await mark_checklist(db, tenant.isp_operator_id, "voucher_generated")
        await db.commit()
        return vouchers
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/batch/{batch_id}", response_model=VoucherListResponse)
async def get_voucher_batch(
    batch_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    query = (
        select(Voucher)
        .where(Voucher.batch_id == batch_id, Voucher.isp_operator_id == tenant.isp_operator_id)
        .order_by(Voucher.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    count_query = select(func.count()).select_from(Voucher).where(Voucher.batch_id == batch_id, Voucher.isp_operator_id == tenant.isp_operator_id)

    result = await db.execute(query)
    vouchers = result.scalars().all()
    count_result = await db.execute(count_query)
    total = count_result.scalar()

    return VoucherListResponse(vouchers=vouchers, total=total)


@router.get("/{voucher_id}", response_model=VoucherResponse, responses={404: {"model": ErrorResponse}})
async def get_voucher(
    voucher_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    result = await db.execute(select(Voucher).where(Voucher.id == voucher_id, Voucher.isp_operator_id == tenant.isp_operator_id))
    voucher = result.scalar_one_or_none()
    if not voucher:
        raise HTTPException(status_code=404, detail="Voucher not found")
    return voucher


@router.put("/{voucher_id}/disable", response_model=VoucherResponse, responses={404: {"model": ErrorResponse}, 400: {"model": ErrorResponse}})
async def disable_voucher(
    voucher_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    try:
        voucher = await disable_voucher_with_disconnect(db, voucher_id, tenant.isp_operator_id)
        return voucher
    except ValueError as e:
        detail = str(e)
        status_code = 404 if "not found" in detail else 400
        raise HTTPException(status_code=status_code, detail=detail)


@router.put("/{voucher_id}/reactivate", response_model=VoucherResponse, responses={404: {"model": ErrorResponse}, 400: {"model": ErrorResponse}})
async def reactivate_voucher(
    voucher_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    try:
        voucher = await restore_voucher_status(db, voucher_id, tenant.isp_operator_id)
        return voucher
    except ValueError as e:
        detail = str(e)
        status_code = 404 if "not found" in detail else 400
        raise HTTPException(status_code=status_code, detail=detail)
