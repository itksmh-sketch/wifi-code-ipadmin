from datetime import datetime, timedelta, timezone
from io import StringIO
import csv
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import CoAEvent, PaymentTransaction
from src.jobs.queue import get_redis_pool
from src.middleware.auth import TenantContext, get_admin_tenant_context
from src.modules.payments.dependencies import get_payment_service
from src.modules.payments.service import PaymentService
from src.modules.payments.types import PaymentMethod, PaymentStatus

router = APIRouter(prefix="/payments", tags=["payments"])


def _payment_filters(
    status: str | None,
    payment_method: str | None,
    site_id: uuid.UUID | None,
    start_date: datetime | None,
    end_date: datetime | None,
):
    clauses = []
    if status:
        clauses.append(PaymentTransaction.status == status)
    if payment_method:
        clauses.append(PaymentTransaction.payment_method == payment_method)
    if site_id:
        clauses.append(PaymentTransaction.site_id == site_id)
    if start_date:
        clauses.append(PaymentTransaction.initiated_at >= start_date)
    if end_date:
        clauses.append(PaymentTransaction.initiated_at <= end_date)
    return clauses


@router.get("")
async def list_payments(
    status: str | None = None,
    payment_method: str | None = None,
    site_id: uuid.UUID | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    filters = _payment_filters(status, payment_method, site_id, start_date, end_date)
    filters.append(PaymentTransaction.isp_operator_id == tenant.isp_operator_id)
    stmt = select(PaymentTransaction).order_by(PaymentTransaction.created_at.desc())
    count_stmt = select(func.count()).select_from(PaymentTransaction)
    if filters:
        stmt = stmt.where(and_(*filters))
        count_stmt = count_stmt.where(and_(*filters))
    stmt = stmt.limit(50).offset((page - 1) * 50)
    rows = (await db.execute(stmt)).scalars().all()
    total = (await db.execute(count_stmt)).scalar() or 0
    return {"items": rows, "page": page, "per_page": 50, "total": total}


@router.get("/summary")
async def payment_summary(db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    today_revenue = (
        await db.execute(
            select(func.coalesce(func.sum(PaymentTransaction.amount_ghs), 0)).where(
                PaymentTransaction.status == PaymentStatus.SUCCESS.value,
                PaymentTransaction.isp_operator_id == tenant.isp_operator_id,
                PaymentTransaction.completed_at >= day_start,
            )
        )
    ).scalar()
    month_revenue = (
        await db.execute(
            select(func.coalesce(func.sum(PaymentTransaction.amount_ghs), 0)).where(
                PaymentTransaction.status == PaymentStatus.SUCCESS.value,
                PaymentTransaction.isp_operator_id == tenant.isp_operator_id,
                PaymentTransaction.completed_at >= month_start,
            )
        )
    ).scalar()
    today_success_count = (
        await db.execute(
            select(func.count()).select_from(PaymentTransaction).where(
                PaymentTransaction.status == PaymentStatus.SUCCESS.value,
                PaymentTransaction.isp_operator_id == tenant.isp_operator_id,
                PaymentTransaction.completed_at >= day_start,
            )
        )
    ).scalar()
    pending_count = (
        await db.execute(
            select(func.count()).select_from(PaymentTransaction).where(
                PaymentTransaction.status == PaymentStatus.PENDING.value,
                PaymentTransaction.isp_operator_id == tenant.isp_operator_id,
            )
        )
    ).scalar()
    failed_count_today = (
        await db.execute(
            select(func.count()).select_from(PaymentTransaction).where(
                PaymentTransaction.status == PaymentStatus.FAILED.value,
                PaymentTransaction.isp_operator_id == tenant.isp_operator_id,
                PaymentTransaction.completed_at >= day_start,
            )
        )
    ).scalar()
    coa_failures = (
        await db.execute(
            select(func.count()).select_from(CoAEvent).where(
                CoAEvent.status == "failed",
                CoAEvent.isp_operator_id == tenant.isp_operator_id,
                CoAEvent.attempt_count >= 3,
            )
        )
    ).scalar()
    return {
        "today_revenue_ghs": float(today_revenue or 0),
        "month_revenue_ghs": float(month_revenue or 0),
        "today_success_count": int(today_success_count or 0),
        "pending_count": int(pending_count or 0),
        "failed_count_today": int(failed_count_today or 0),
        "coa_failures": int(coa_failures or 0),
    }


@router.put("/{payment_id}/verify")
async def verify_payment(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    payment_service: PaymentService = Depends(get_payment_service),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    tx = (
        await db.execute(select(PaymentTransaction).where(PaymentTransaction.id == payment_id, PaymentTransaction.isp_operator_id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if not tx:
        raise HTTPException(status_code=404, detail="Payment not found")
    if not tx.provider_reference:
        raise HTTPException(status_code=400, detail="Payment has no provider reference")
    return await payment_service.refresh_transaction_status(db, tx=tx, force=True)


@router.put("/{payment_id}/refund")
async def refund_payment(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    payment_service: PaymentService = Depends(get_payment_service),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    tx = (
        await db.execute(select(PaymentTransaction).where(PaymentTransaction.id == payment_id, PaymentTransaction.isp_operator_id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if not tx:
        raise HTTPException(status_code=404, detail="Payment not found")
    old_status = tx.status
    tx.status = PaymentStatus.REFUNDED.value
    tx.completed_at = datetime.now(timezone.utc)
    payment_service._log_status_change(tx, old_status, tx.status, "manual")
    await db.commit()
    await db.refresh(tx)
    return tx


@router.post("/reconcile")
async def trigger_reconcile(_=Depends(get_admin_tenant_context)):
    redis = await get_redis_pool()
    job = await redis.enqueue_job("run_payment_reconciliation")
    return {"queued": True, "job_id": job.job_id if job else None}


@router.get("/export")
async def export_payments_csv(
    format: str = "csv",
    status: str | None = None,
    payment_method: str | None = None,
    site_id: uuid.UUID | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    if format != "csv":
        raise HTTPException(status_code=400, detail="Only csv format is supported")
    filters = _payment_filters(status, payment_method, site_id, start_date, end_date)
    filters.append(PaymentTransaction.isp_operator_id == tenant.isp_operator_id)
    stmt = select(PaymentTransaction).order_by(PaymentTransaction.created_at.desc())
    if filters:
        stmt = stmt.where(and_(*filters))
    rows = (await db.execute(stmt)).scalars().all()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "internal_reference",
            "provider_reference",
            "status",
            "payment_method",
            "provider",
            "amount_ghs",
            "phone_number",
            "initiated_at",
            "completed_at",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.id,
                r.internal_reference,
                r.provider_reference,
                r.status,
                r.payment_method,
                r.provider,
                r.amount_ghs,
                r.phone_number,
                r.initiated_at,
                r.completed_at,
            ]
        )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=payments.csv"},
    )


@router.get("/{payment_id}")
async def get_payment(payment_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    tx = (
        await db.execute(select(PaymentTransaction).where(PaymentTransaction.id == payment_id, PaymentTransaction.isp_operator_id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if not tx:
        raise HTTPException(status_code=404, detail="Payment not found")
    return tx
