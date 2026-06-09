from __future__ import annotations
import uuid
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal

import httpx
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.models import (
    ISPOperator,
    OperatorInvoice,
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
        amount_ghs=operator.monthly_fee_ghs,
        status="issued",
        issued_at=now,
        due_at=due_at,
    )
    db.add(invoice)

    event = OperatorBillingEvent(
        isp_operator_id=operator.id,
        event_type="invoice_issued",
        description=f"Invoice {invoice_number} issued for GHS {operator.monthly_fee_ghs}.",
        event_metadata={"invoice_number": invoice_number, "amount_ghs": str(operator.monthly_fee_ghs)},
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
