from __future__ import annotations
import uuid
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal, ROUND_HALF_UP

import httpx
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.models import (
    ISPOperator,
    OperatorInvoice,
    OperatorInvoiceLineItem,
    OperatorBillingEvent,
)


async def get_next_invoice_number(db: AsyncSession) -> str:
    year = datetime.now(timezone.utc).year
    count = (
        await db.execute(
            select(func.count()).select_from(OperatorInvoice).where(
                func.extract("year", OperatorInvoice.created_at) == year
            )
        )
    ).scalar() or 0
    return f"INV-{year}-{str(count + 1).zfill(3)}"


def line_amount(quantity: Decimal, unit_price_ghs: Decimal) -> Decimal:
    """quantity x unit price, rounded to the 2dp the amount column stores.

    Rounding happens per line, not on the invoice total, so the stored total is
    always exactly the sum of the amounts shown against each line — an invoice
    can never display lines that do not add up to what is charged.
    """
    return (Decimal(quantity) * Decimal(unit_price_ghs)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def recompute_invoice_total(db: AsyncSession, invoice: OperatorInvoice) -> Decimal:
    """Set invoice.amount_ghs to the sum of its line items. Caller commits."""
    total = (
        await db.execute(
            select(func.coalesce(func.sum(OperatorInvoiceLineItem.amount_ghs), 0)).where(
                OperatorInvoiceLineItem.invoice_id == invoice.id
            )
        )
    ).scalar() or Decimal("0")
    total = Decimal(total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    invoice.amount_ghs = total
    await db.flush()
    return total


async def add_line_item(
    db: AsyncSession,
    invoice: OperatorInvoice,
    *,
    kind: str,
    description: str,
    quantity: Decimal = Decimal("1"),
    unit_price_ghs: Decimal,
    metadata: dict | None = None,
    sort_order: int = 0,
) -> OperatorInvoiceLineItem:
    """Add a charge to an invoice and recompute its total.

    THE ONLY SUPPORTED WAY TO ADD A LINE. It recomputes `invoice.amount_ghs` in
    the same flush, so the stored total can never drift from what the lines sum
    to. Inserting an OperatorInvoiceLineItem directly, or assigning
    `invoice.amount_ghs` by hand, breaks that guarantee — don't.

    Caller commits.
    """
    line = OperatorInvoiceLineItem(
        invoice_id=invoice.id,
        kind=kind,
        description=description,
        quantity=Decimal(quantity),
        unit_price_ghs=Decimal(unit_price_ghs),
        amount_ghs=line_amount(quantity, unit_price_ghs),
        line_metadata=metadata,
        sort_order=sort_order,
    )
    db.add(line)
    await db.flush()
    await recompute_invoice_total(db, invoice)
    return line


async def create_invoice(
    db: AsyncSession,
    operator: ISPOperator,
    period_start: date,
    period_end: date,
) -> OperatorInvoice:
    now = datetime.now(timezone.utc)
    invoice_number = await get_next_invoice_number(db)
    due_at = datetime(period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc) + timedelta(days=7)

    invoice = OperatorInvoice(
        isp_operator_id=operator.id,
        invoice_number=invoice_number,
        period_start=datetime(period_start.year, period_start.month, period_start.day, tzinfo=timezone.utc),
        period_end=datetime(period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc),
        # Starts at zero and is set by the subscription line below. Never assign
        # this directly — see add_line_item.
        amount_ghs=Decimal("0.00"),
        status="issued",
        issued_at=now,
        due_at=due_at,
    )
    db.add(invoice)
    await db.flush()

    await add_line_item(
        db,
        invoice,
        kind="subscription",
        description=f"Monthly subscription — {period_start:%d %b %Y} to {period_end:%d %b %Y}",
        quantity=Decimal("1"),
        unit_price_ghs=Decimal(operator.monthly_fee_ghs),
        sort_order=0,
    )

    event = OperatorBillingEvent(
        isp_operator_id=operator.id,
        event_type="invoice_issued",
        # Reads the recomputed total, not the operator's fee, so the event can
        # never disagree with the invoice once there is more than one line.
        description=f"Invoice {invoice_number} issued for GHS {invoice.amount_ghs}.",
        event_metadata={"invoice_number": invoice_number, "amount_ghs": str(invoice.amount_ghs)},
    )
    db.add(event)
    await db.flush()
    return invoice


async def get_outstanding_invoice(db: AsyncSession, operator_id: uuid.UUID) -> OperatorInvoice | None:
    result = await db.execute(
        select(OperatorInvoice)
        .where(
            OperatorInvoice.isp_operator_id == operator_id,
            OperatorInvoice.status.in_(["issued", "overdue"]),
        )
        .order_by(OperatorInvoice.created_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def initiate_invoice_payment(
    db: AsyncSession,
    invoice: OperatorInvoice,
    operator: ISPOperator,
) -> str:
    """Returns the Paystack authorization_url."""
    settings = get_settings()
    if not settings.platform_billing_paystack_secret_key:
        raise ValueError("Platform billing Paystack keys not configured")

    amount_pesewas = int(invoice.amount_ghs * 100)
    reference = f"INV-{invoice.id}"
    callback_url = f"{settings.platform_app_url}/billing/payment-callback"

    payload = {
        "email": operator.contact_email,
        "amount": amount_pesewas,
        "reference": reference,
        "callback_url": callback_url,
        "metadata": {
            "invoice_id": str(invoice.id),
            "operator_id": str(operator.id),
        },
    }

    async with httpx.AsyncClient(timeout=15, base_url="https://api.paystack.co") as client:
        resp = await client.post(
            "/transaction/initialize",
            json=payload,
            headers={"Authorization": f"Bearer {settings.platform_billing_paystack_secret_key}"},
        )

    data = resp.json()
    if not data.get("status"):
        raise ValueError(f"Paystack error: {data.get('message', 'unknown')}")

    auth_url: str = data["data"]["authorization_url"]

    # Store the URL on the invoice
    invoice.paystack_payment_url = auth_url
    await db.commit()

    return auth_url


async def mark_invoice_paid(
    db: AsyncSession,
    invoice: OperatorInvoice,
    payment_reference: str,
) -> OperatorInvoice:
    now = datetime.now(timezone.utc)
    invoice.status = "paid"
    invoice.paid_at = now
    invoice.payment_reference = payment_reference

    event = OperatorBillingEvent(
        isp_operator_id=invoice.isp_operator_id,
        event_type="invoice_paid",
        description=f"Invoice {invoice.invoice_number} paid. Reference: {payment_reference}.",
        event_metadata={"invoice_number": invoice.invoice_number, "payment_reference": payment_reference},
    )
    db.add(event)
    await db.flush()
    return invoice


async def reactivate_operator(db: AsyncSession, operator: ISPOperator) -> ISPOperator:
    operator.status = "approved"
    operator.billing_status = "active"

    event = OperatorBillingEvent(
        isp_operator_id=operator.id,
        event_type="reactivated",
        description=f"{operator.name} reactivated after payment.",
        event_metadata={},
    )
    db.add(event)
    await db.flush()
    return operator
