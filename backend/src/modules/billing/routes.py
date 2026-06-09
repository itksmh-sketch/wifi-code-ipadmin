from __future__ import annotations
import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import ISPOperator, OperatorInvoice
from src.middleware.auth import TenantContext, get_admin_tenant_context
from src.modules.billing import service
from src.modules.billing.schemas import BillingStatusResponse, InvoiceResponse, PayInvoiceResponse

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/status", response_model=BillingStatusResponse)
async def billing_status(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    operator = (
        await db.execute(select(ISPOperator).where(ISPOperator.id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if not operator:
        raise HTTPException(404, "Operator not found")

    now = datetime.now(timezone.utc)
    days_remaining = None
    if operator.billing_status == "trial" and operator.trial_ends_at:
        delta = operator.trial_ends_at - now
        days_remaining = max(0, delta.days)

    outstanding = await service.get_outstanding_invoice(db, operator.id)

    return BillingStatusResponse(
        billing_status=operator.billing_status,
        trial_ends_at=operator.trial_ends_at,
        trial_days_remaining=days_remaining,
        has_outstanding_invoice=outstanding is not None,
        outstanding_amount_ghs=outstanding.amount_ghs if outstanding else None,
    )


@router.get("/invoices", response_model=List[InvoiceResponse])
async def list_invoices(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    result = await db.execute(
        select(OperatorInvoice)
        .where(OperatorInvoice.isp_operator_id == tenant.isp_operator_id)
        .order_by(OperatorInvoice.created_at.desc())
    )
    return result.scalars().all()


@router.get("/invoices/{invoice_id}", response_model=InvoiceResponse)
async def get_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    invoice = (
        await db.execute(
            select(OperatorInvoice).where(
                OperatorInvoice.id == invoice_id,
                OperatorInvoice.isp_operator_id == tenant.isp_operator_id,
            )
        )
    ).scalar_one_or_none()
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    return invoice


@router.post("/invoices/{invoice_id}/pay", response_model=PayInvoiceResponse)
async def pay_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    invoice = (
        await db.execute(
            select(OperatorInvoice).where(
                OperatorInvoice.id == invoice_id,
                OperatorInvoice.isp_operator_id == tenant.isp_operator_id,
            )
        )
    ).scalar_one_or_none()
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    if invoice.status not in ("issued", "overdue"):
        raise HTTPException(400, f"Invoice cannot be paid in status '{invoice.status}'")

    operator = (
        await db.execute(select(ISPOperator).where(ISPOperator.id == tenant.isp_operator_id))
    ).scalar_one_or_none()

    try:
        redirect_url = await service.initiate_invoice_payment(db, invoice, operator)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return PayInvoiceResponse(redirect_url=redirect_url, invoice_id=invoice.id)


@router.get("/onboarding-checklist")
async def get_onboarding_checklist(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    from src.modules.onboarding import get_checklist
    return await get_checklist(db, tenant.isp_operator_id)


@router.get("/payment-callback", response_class=HTMLResponse)
async def payment_callback(request: Request):
    # Paystack redirects here after payment; actual confirmation via webhook.
    reference = request.query_params.get("trxref") or request.query_params.get("reference", "")
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Payment Processing</title>
<meta http-equiv="refresh" content="3;url=/admin/billing">
<style>body{{font-family:sans-serif;text-align:center;padding:60px}}</style>
</head><body>
<h2>Payment received!</h2>
<p>Reference: <code>{reference}</code></p>
<p>Your account will be updated shortly. Redirecting to billing page...</p>
</body></html>""",
        status_code=200,
    )
