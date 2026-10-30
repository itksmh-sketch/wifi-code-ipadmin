from __future__ import annotations
import base64
import hashlib
import html
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
    SMSMessage,
    SMSUsageRecord,
    Voucher,
)
from src.middleware.auth import TenantContext, get_admin_tenant_context, require_recent_pin
from src.middleware.permissions import BILLING_READ, READ, SECRETS, require_perm
from src.modules import audit
from src.modules.audit.redact import scrub
from src.modules.billing import service
from src.modules.billing.schemas import BillingStatusResponse, InvoiceResponse, PayInvoiceResponse

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/status", response_model=BillingStatusResponse, dependencies=[Depends(require_perm(READ))])
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


@router.get("/invoices", response_model=List[InvoiceResponse], dependencies=[Depends(require_perm(BILLING_READ))])
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


@router.get("/invoices/{invoice_id}", response_model=InvoiceResponse, dependencies=[Depends(require_perm(BILLING_READ))])
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
    dependencies=[Depends(audit.audited), Depends(require_perm(SECRETS)), Depends(require_recent_pin)],
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

    detail = {"invoice_number": invoice.invoice_number, "amount_ghs": invoice.amount_ghs,
              "invoice_status": invoice.status,
              "reference": invoice.payment_reference or f"INV-{invoice.id}"}
    previous_url = invoice.paystack_payment_url
    try:
        redirect_url = await service.initiate_invoice_payment(db, invoice, operator)
    except Exception as exc:
        # Opens a checkout with the platform's Paystack key: an error that
        # echoes it is scrubbed before it is stored.
        await db.rollback()
        error = scrub(exc, await _platform_key_values(db))
        await audit.record_detached(
            "billing.invoice_pay", target_type="invoice", target_id=invoice_id,
            summary=f"Could not start paying invoice {detail['invoice_number']} (GHS {detail['amount_ghs']})",
            detail={**detail, "outcome": "failed", "error": error},
        )
        if isinstance(exc, ValueError):
            # The platform's key is never the operator's to see, even in part.
            raise HTTPException(400, error)
        raise

    # The checkout link itself is not stored in the row. The same link coming
    # back means the open checkout was handed out again, not a new one.
    reused = previous_url is not None and redirect_url == previous_url
    await audit.record_detached(
        "billing.invoice_pay", target_type="invoice", target_id=invoice_id,
        summary=f"Started paying invoice {detail['invoice_number']} (GHS {detail['amount_ghs']})",
        detail={**detail, "outcome": "checkout_reused" if reused else "checkout_opened",
                "reference": invoice.payment_reference or detail["reference"]},
    )
    return PayInvoiceResponse(redirect_url=redirect_url, invoice_id=invoice.id)


async def _platform_key_values(db: AsyncSession) -> list[str]:
    from src.modules.platform.payment_credentials_service import resolve_paystack_keys

    try:
        keys = await resolve_paystack_keys(db)
    except Exception:
        return []
    return [v for v in (getattr(keys, name, None) for name in ("secret_key", "public_key", "webhook_secret")) if v]


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


@router.get("/transactions", dependencies=[Depends(require_perm(BILLING_READ))])
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


# Arkesel statuses that mean the message will never arrive.
_UNDELIVERED = {"NOT_DELIVERED", "UNDELIVERED", "EXPIRED", "PROHIBITED", "REJECTED", "FAILED"}


def _delivery(msg: SMSMessage | None) -> dict | None:
    """What the SMS Usage tab shows about one message. None = not tracked
    (sent before delivery tracking began)."""
    if msg is None:
        return None
    status = (msg.provider_status or "").upper()
    if status == "DELIVERED":
        state = "delivered"
    elif status in _UNDELIVERED:
        state = "not_delivered"
    elif msg.provider_sent_at is not None:
        state = "sent_to_network"
    else:
        state = "waiting"
    delay = None
    if msg.provider_sent_at is not None and msg.accepted_at is not None:
        delay = max(0, int((msg.provider_sent_at - msg.accepted_at).total_seconds()))
    return {
        "state": state,
        "provider_status": msg.provider_status,
        "accepted_at": msg.accepted_at.isoformat() if msg.accepted_at else None,
        "sent_to_network_at": msg.provider_sent_at.isoformat() if msg.provider_sent_at else None,
        "network_delay_seconds": delay,
        "final_status_at": msg.final_status_at.isoformat() if msg.final_status_at else None,
    }


@router.get("/sms-usage", dependencies=[Depends(require_perm(BILLING_READ))])
async def list_sms_usage(
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    """The operator's own platform-gateway SMS charges, newest first.

    The invoice a record landed on is two hops away (usage record -> line item
    -> invoice), so both joins are outer: an unbilled record has no line item
    and therefore no invoice, which is a normal state, not missing data.

    Delivery status comes from sms_messages (migration 054), matched on the
    provider's message id; also an outer join, since records sent before
    delivery tracking began (2026-10-05) have no row there."""
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
            select(SMSUsageRecord, OperatorInvoice.invoice_number, OperatorInvoice.issued_at, SMSMessage)
            .outerjoin(
                SMSMessage,
                (SMSMessage.provider == "arkesel_platform")
                & (SMSMessage.provider_reference == SMSUsageRecord.provider_reference)
                & (SMSMessage.isp_operator_id == SMSUsageRecord.isp_operator_id),
            )
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
                "delivery": _delivery(msg),
            }
            for rec, invoice_number, issued_at, msg in rows
        ],
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
    }


@router.get("/onboarding-checklist", dependencies=[Depends(require_perm(BILLING_READ))])
async def get_onboarding_checklist(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    from src.modules.onboarding import get_checklist
    return await get_checklist(db, tenant.isp_operator_id)


# The return page's only style, and its CSP hash: the policy allows exactly
# this stylesheet and nothing else (no script, no other source of any kind).
_CALLBACK_STYLE = "body{font-family:sans-serif;text-align:center;padding:60px}"
_CALLBACK_CSP = (
    "default-src 'none'; "
    f"style-src 'sha256-{base64.b64encode(hashlib.sha256(_CALLBACK_STYLE.encode()).digest()).decode()}'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


@router.get("/payment-callback", response_class=HTMLResponse)
async def payment_callback(request: Request):
    """Where Paystack sends the payer's browser after an invoice payment.

    The query string is attacker-controlled (anyone can craft this link), and
    this page shares an origin with /admin and /platform, whose tokens live in
    localStorage: the reference is escaped, and the CSP forbids any script.
    It says nothing about the outcome either: the redirect proves nothing (the
    payer may have cancelled or the charge failed). The invoice changes only on
    the verified webhook.
    """
    reference = request.query_params.get("trxref") or request.query_params.get("reference", "")
    reference_line = f"<p>Reference: <code>{html.escape(reference)}</code></p>\n" if reference else ""
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Returning to billing</title>
<meta http-equiv="refresh" content="3;url=/admin/billing">
<style>{_CALLBACK_STYLE}</style>
</head><body>
<h2>Returning to your billing page</h2>
{reference_line}<p>If your payment went through, your invoice will show as paid once the payment provider confirms it. Redirecting to billing...</p>
</body></html>""",
        status_code=200,
        headers={"Content-Security-Policy": _CALLBACK_CSP},
    )
