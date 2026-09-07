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


# Paystack charges in the currency's minor unit: GHS -> pesewas, x100.
PESEWAS_PER_GHS = Decimal("100")
# Paystack rejects anything smaller; the operator-side provider enforces the same
# floor (see modules/payments/providers/paystack.py).
PAYSTACK_MINIMUM_GHS = Decimal("1.00")


def ghs_to_pesewas(amount_ghs: Decimal) -> int:
    """GHS -> pesewas, the unit Paystack quotes and reports in.

    THE single conversion for platform billing. Charge initiation and webhook
    amount verification must agree exactly: if one truncated where the other
    rounded, a genuinely underpaid charge could pass verification as
    "close enough", or a correct payment could be rejected.
    """
    return int((Decimal(amount_ghs) * PESEWAS_PER_GHS).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


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
    # Resolved from platform_payment_credentials, falling back to .env — the same
    # source the webhook verifies signatures with, so initiation and verification
    # can never end up on different keys.
    from src.modules.platform.payment_credentials_service import resolve_paystack_keys
    from src.modules.platform.settings_service import get_setting

    keys = await resolve_paystack_keys(db)
    if not keys.is_configured:
        raise ValueError("Platform billing Paystack keys not configured")

    # Refuse below Paystack's floor here rather than sending a doomed request:
    # a zero-amount invoice is the shape the operator-creation bug produced, and
    # it fails at Paystack with an opaque error the operator cannot act on.
    if invoice.amount_ghs < PAYSTACK_MINIMUM_GHS:
        raise ValueError(
            f"Invoice {invoice.invoice_number} is GHS {invoice.amount_ghs}, below Paystack's "
            f"GHS {PAYSTACK_MINIMUM_GHS} minimum charge — it cannot be paid online."
        )

    amount_pesewas = ghs_to_pesewas(invoice.amount_ghs)
    reference = f"INV-{invoice.id}"
    # /api/v1 prefix included: the billing router is mounted under it, so the
    # bare "/billing/payment-callback" this used to send returned 404 and every
    # payer landed on an error page after paying.
    app_url = (await get_setting(db, "platform_app_url")).rstrip("/")
    callback_url = f"{app_url}/api/v1/billing/payment-callback"

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
            headers={"Authorization": f"Bearer {keys.secret_key}"},
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
    amount_charged_ghs: Decimal | None = None,
) -> OperatorInvoice:
    now = datetime.now(timezone.utc)
    invoice.status = "paid"
    invoice.paid_at = now
    invoice.payment_reference = payment_reference

    # What was actually charged is recorded even when it exceeds the invoice.
    # Overpayment is accepted rather than credited — there is no credit balance —
    # so the event metadata is the only record that the excess happened.
    metadata = {"invoice_number": invoice.invoice_number, "payment_reference": payment_reference}
    if amount_charged_ghs is not None:
        metadata["amount_charged_ghs"] = str(amount_charged_ghs)
        metadata["invoice_amount_ghs"] = str(invoice.amount_ghs)

    event = OperatorBillingEvent(
        isp_operator_id=invoice.isp_operator_id,
        event_type="invoice_paid",
        description=f"Invoice {invoice.invoice_number} paid. Reference: {payment_reference}.",
        event_metadata=metadata,
    )
    db.add(event)
    await db.flush()
    return invoice


async def normalise_billing_status(db: AsyncSession, operator: ISPOperator) -> bool:
    """Put a paid-up operator back to billing_status 'active'. Caller commits.

    Runs on *any* successful payment, independent of suspension. Previously this
    only happened as a side effect of reactivation, so an operator who paid
    inside the grace period — past_due but not yet suspended — stayed past_due
    forever, and generate_monthly_invoices (which selects billing_status ==
    'active') silently never invoiced them again.

    Returns True if it changed anything.
    """
    if operator.billing_status == "active":
        return False
    operator.billing_status = "active"
    await db.flush()
    return True


def is_billing_suspension(operator: ISPOperator) -> bool:
    """Whether a payment is allowed to lift this operator's suspension.

    Only a suspension raised by the billing system. NULL means we do not know
    why they are suspended, and unknown is treated as 'no' — wrongly reactivating
    an operator suspended for abuse is silent and bad, whereas leaving a paid-up
    operator suspended is visible and manually recoverable.
    """
    return operator.status == "suspended" and operator.suspension_reason == "billing"


async def reactivate_operator(db: AsyncSession, operator: ISPOperator) -> ISPOperator:
    """Lift a *billing* suspension. Callers must gate on is_billing_suspension()."""
    operator.status = "approved"
    operator.billing_status = "active"
    # Clear the tag with the suspension it describes, so an active operator never
    # carries a stale reason.
    operator.suspension_reason = None

    event = OperatorBillingEvent(
        isp_operator_id=operator.id,
        event_type="reactivated",
        description=f"{operator.name} reactivated after payment.",
        event_metadata={"reason": "billing_suspension_cleared_by_payment"},
    )
    db.add(event)
    await db.flush()
    return operator
