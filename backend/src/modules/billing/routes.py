from __future__ import annotations
import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import (
    ISPOperator,
    OperatorInvoice,
    OperatorInvoiceLineItem,
    PaymentTransaction,
    SMSUsageRecord,
    Voucher,
)
from src.middleware.auth import TenantContext, get_admin_tenant_context, require_recent_pin
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
        account_status=operator.status,
        is_suspended=operator.status == "suspended",
        suspension_reason=operator.suspension_reason,
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


# The one genuinely money-moving endpoint an operator admin can reach: it
# opens a live payment session against the operator's own funds.
@router.post(
    "/invoices/{invoice_id}/pay",
    response_model=PayInvoiceResponse,
    dependencies=[Depends(require_recent_pin)],
)
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


# --- Read-only history (operator's own records) ---
#
# Both endpoints are strictly read-only, tenant-scoped, and exclude
# is_diagnostic rows — an internal test artifact is never an operator's
# business. The scope predicates are built once per endpoint and applied to
# BOTH the count and the page query, so a total can never disagree with the
# rows it is counting.

_HISTORY_PAGE_SIZE_MAX = 200

# Raw enum -> what an operator should actually read. Lives here rather than in
# the frontend so the label has one definition regardless of client; the
# frontend only decides colour.
_TRANSACTION_STATUS_LABELS = {
    "pending": "Pending",
    "success": "Successful",
    "failed": "Failed",
    "refunded": "Refunded",
    "reversed": "Reversed",
}


def _page_bounds(page: int, page_size: int, total_count: int) -> tuple[int, int, int]:
    """Clamp paging inputs and derive total_pages. Never trusts the client; a
    page past the last one yields an empty list rather than an error."""
    page = max(1, page)
    page_size = max(1, min(page_size, _HISTORY_PAGE_SIZE_MAX))
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    return page, page_size, total_pages


@router.get("/transactions")
async def list_transactions(
    page: int = 1,
    page_size: int = 50,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    """The operator's own payment transactions, newest first.

    start_date/end_date filter on initiated_at, named to match the existing
    convention in payments.routes._payment_filters rather than inventing a
    from/to spelling. They can only ever narrow the tenant scope below — the
    operator predicate is part of the same tuple, so no date range can widen
    past it."""
    scope = [
        PaymentTransaction.isp_operator_id == tenant.isp_operator_id,
        PaymentTransaction.is_diagnostic.is_(False),
    ]
    if start_date:
        scope.append(PaymentTransaction.initiated_at >= start_date)
    if end_date:
        scope.append(PaymentTransaction.initiated_at <= end_date)
    scope = tuple(scope)

    total_count = (
        await db.execute(select(func.count()).select_from(PaymentTransaction).where(*scope))
    ).scalar() or 0
    page, page_size, total_pages = _page_bounds(page, page_size, total_count)

    rows = (
        await db.execute(
            select(PaymentTransaction, Voucher.code)
            .outerjoin(Voucher, Voucher.id == PaymentTransaction.voucher_id)
            .where(*scope)
            .order_by(PaymentTransaction.initiated_at.desc(), PaymentTransaction.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()

    return {
        "transactions": [
            {
                "id": str(tx.id),
                "initiated_at": tx.initiated_at.isoformat() if tx.initiated_at else None,
                "amount_ghs": float(tx.amount_ghs),
                "payment_method": tx.payment_method,
                "status": tx.status,
                "status_label": _TRANSACTION_STATUS_LABELS.get(tx.status, tx.status),
                # Only a successful charge produces one; null otherwise.
                "voucher_code": code,
            }
            for tx, code in rows
        ],
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
    }


@router.get("/sms-usage")
async def list_sms_usage(
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    """The operator's own platform-gateway SMS charges, newest first.

    The invoice a record landed on is two hops away (usage record -> line item
    -> invoice), so both joins are outer: an unbilled record has no line item
    and therefore no invoice, which is a normal state, not missing data."""
    scope = (
        SMSUsageRecord.isp_operator_id == tenant.isp_operator_id,
        SMSUsageRecord.is_diagnostic.is_(False),
    )

    total_count = (
        await db.execute(select(func.count()).select_from(SMSUsageRecord).where(*scope))
    ).scalar() or 0
    page, page_size, total_pages = _page_bounds(page, page_size, total_count)

    rows = (
        await db.execute(
            select(SMSUsageRecord, OperatorInvoice.invoice_number, OperatorInvoice.issued_at)
            .outerjoin(
                OperatorInvoiceLineItem,
                OperatorInvoiceLineItem.id == SMSUsageRecord.invoice_line_item_id,
            )
            .outerjoin(OperatorInvoice, OperatorInvoice.id == OperatorInvoiceLineItem.invoice_id)
            .where(*scope)
            .order_by(SMSUsageRecord.sent_at.desc(), SMSUsageRecord.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()

    return {
        "records": [
            {
                "id": str(rec.id),
                "sent_at": rec.sent_at.isoformat() if rec.sent_at else None,
                "segment_count": rec.segment_count,
                "rate_ghs_per_segment": str(rec.rate_ghs_per_segment),
                "amount_ghs": float(rec.amount_ghs),
                "invoice_number": invoice_number,
                "invoice_issued_at": issued_at.isoformat() if issued_at else None,
            }
            for rec, invoice_number, issued_at in rows
        ],
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
    }


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
