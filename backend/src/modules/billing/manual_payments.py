"""Payments the platform owner received outside Paystack — mobile money, a bank
transfer, cash — recorded by hand against one invoice, and their reversal.

A payment is for the invoice's full amount: there are no part payments and no
credit balance, so an amount that differs is refused rather than guessed at.
Recording one does exactly what a Paystack payment does (service.settle_invoice):
the invoice is paid, billing goes back to active, and a billing suspension is
lifted. A wrong entry is never edited or deleted (the table refuses both,
migration 064): it is reversed, once, with a reason. A reversal can't itself
be reversed.

Reversing puts the invoice back to 'issued' — not 'overdue', even when it is
past due — so the next billing_enforcement run moves it on the usual way: the
overdue mark, the grace-period notice, and suspension only after the grace
period. The operator's own status is not touched here.

Every entry is audited with the platform owner as the actor (the route runs
under audit.audited).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ISPOperator, OperatorBillingEvent, OperatorInvoice, OperatorInvoicePayment
from src.modules import audit
from src.modules.billing.service import settle_invoice

METHODS = {"momo": "Mobile money", "bank": "Bank transfer", "cash": "Cash"}
# Mobile money and bank transfers always have a transaction reference.
REFERENCE_REQUIRED = {"momo", "bank"}
PAYABLE_STATUSES = ("issued", "overdue")
# How far back a received date may go. Further than this is a typo.
MAX_BACKDATE_DAYS = 366
NOTE_MAX = 500
REFERENCE_MAX = 120


class ManualPaymentError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class Recorded:
    payment: OperatorInvoicePayment
    invoice: OperatorInvoice
    operator: ISPOperator
    reactivated: bool


def payment_reference_for(payment_id: uuid.UUID) -> str:
    """What invoice.payment_reference holds for an invoice paid by hand. Never
    starts with INV-, so the Paystack webhook's reference lookup can't match it."""
    return f"manual:{payment_id}"


def _clean(text: str | None, limit: int, field: str) -> str | None:
    text = (text or "").strip()
    if len(text) > limit:
        raise ManualPaymentError(400, f"{field} is too long (at most {limit} characters).")
    return text or None


async def _locked_operator_and_invoice(db: AsyncSession, operator_id: uuid.UUID,
                                       invoice_id: uuid.UUID) -> tuple[ISPOperator, OperatorInvoice]:
    """Operator first, then invoice — the order the Paystack webhook locks in,
    so the two can't deadlock, and whichever comes second sees the other's
    payment."""
    operator = (
        await db.execute(select(ISPOperator).where(ISPOperator.id == operator_id).with_for_update())
    ).scalar_one_or_none()
    if operator is None:
        raise ManualPaymentError(404, "Operator not found")
    invoice = (
        await db.execute(
            select(OperatorInvoice)
            .where(OperatorInvoice.id == invoice_id, OperatorInvoice.isp_operator_id == operator.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if invoice is None:
        # Also another operator's invoice: same answer, nothing about it leaks.
        raise ManualPaymentError(404, "Invoice not found")
    return operator, invoice


async def record_payment(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    operator_id: uuid.UUID,
    invoice_id: uuid.UUID,
    amount_ghs: Decimal,
    method: str,
    reference: str | None,
    received_on: date,
    note: str | None,
    today: date | None = None,
) -> Recorded:
    """Record a full payment and settle the invoice. The caller commits, then
    sends the reactivation notice if `reactivated`."""
    today = today or datetime.now(timezone.utc).date()
    if method not in METHODS:
        raise ManualPaymentError(400, "Method must be mobile money, bank transfer or cash.")
    reference = _clean(reference, REFERENCE_MAX, "Reference")
    note = _clean(note, NOTE_MAX, "Note")
    if method in REFERENCE_REQUIRED and not reference:
        raise ManualPaymentError(400, f"A {METHODS[method].lower()} payment needs its transaction reference.")
    if received_on > today:
        raise ManualPaymentError(400, "The date received can't be in the future.")
    if received_on < today - timedelta(days=MAX_BACKDATE_DAYS):
        raise ManualPaymentError(400, "The date received is more than a year ago. Check the date.")

    operator, invoice = await _locked_operator_and_invoice(db, operator_id, invoice_id)
    if invoice.status not in PAYABLE_STATUSES:
        raise ManualPaymentError(409, f"Invoice {invoice.invoice_number} is {invoice.status}: there is nothing to pay.")
    amount = Decimal(amount_ghs).quantize(Decimal("0.01"))
    if amount != invoice.amount_ghs:
        raise ManualPaymentError(
            400, f"Record the invoice's full amount, GHS {invoice.amount_ghs}. Part payments can't be recorded.")

    payment = OperatorInvoicePayment(
        id=uuid.uuid4(), isp_operator_id=operator.id, invoice_id=invoice.id, kind="payment", amount_ghs=amount,
        method=method, reference=reference, received_on=received_on, note=note,
        recorded_by_platform_owner_id=owner_id,
    )
    db.add(payment)
    await db.flush()

    reactivated = await settle_invoice(
        db, invoice, operator,
        payment_reference=payment_reference_for(payment.id),
        amount_charged_ghs=amount,
        # The day the money came in, so it counts in the month it was received.
        paid_at=datetime.combine(received_on, time(12, 0), tzinfo=timezone.utc),
        detail={"recorded_by_hand": True, "payment_id": str(payment.id), "method": method,
                "reference": reference, "received_on": received_on.isoformat(), "note": note},
        by_system=False,
    )
    return Recorded(payment=payment, invoice=invoice, operator=operator, reactivated=reactivated)


async def reverse_payment(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    payment_id: uuid.UUID,
    reason: str,
    today: date | None = None,
) -> tuple[OperatorInvoicePayment, OperatorInvoice]:
    """Reverse a recorded payment: the invoice is unpaid again. The caller commits."""
    today = today or datetime.now(timezone.utc).date()
    reason = _clean(reason, NOTE_MAX, "Reason")
    if not reason:
        raise ManualPaymentError(400, "Say why the payment is being reversed.")

    target = await db.get(OperatorInvoicePayment, payment_id)
    if target is None:
        raise ManualPaymentError(404, "Payment not found")
    if target.kind != "payment":
        raise ManualPaymentError(409, "A reversal can't be reversed. Record the payment again instead.")

    operator, invoice = await _locked_operator_and_invoice(db, target.isp_operator_id, target.invoice_id)
    already = (
        await db.execute(select(OperatorInvoicePayment.id).where(OperatorInvoicePayment.reverses_payment_id == target.id))
    ).scalar_one_or_none()
    if already is not None:
        raise ManualPaymentError(409, "This payment has already been reversed.")
    if invoice.status != "paid" or invoice.payment_reference != payment_reference_for(target.id):
        # Only the payment that settled the invoice can be reversed; anything
        # else would leave the invoice's state and the ledger disagreeing.
        raise ManualPaymentError(409, f"Invoice {invoice.invoice_number} was not settled by this payment.")

    reversal = OperatorInvoicePayment(
        id=uuid.uuid4(), isp_operator_id=operator.id, invoice_id=invoice.id, kind="reversal",
        amount_ghs=target.amount_ghs, received_on=today, note=reason, reverses_payment_id=target.id,
        recorded_by_platform_owner_id=owner_id,
    )
    db.add(reversal)
    await db.flush()

    audit.record(
        db, "invoice.payment_reverse", isp_operator_id=operator.id, target_type="operator_invoice",
        target_id=invoice.id, summary=f"Reversed the {METHODS[target.method].lower()} payment on invoice "
                                      f"{invoice.invoice_number} (GHS {target.amount_ghs})",
        changes={"status": {"before": invoice.status, "after": "issued"}},
        detail={"invoice_number": invoice.invoice_number, "payment_id": str(target.id),
                "reversal_id": str(reversal.id), "method": target.method, "reference": target.reference,
                "received_on": target.received_on.isoformat(), "amount_ghs": target.amount_ghs, "reason": reason},
    )
    db.add(OperatorBillingEvent(
        isp_operator_id=operator.id,
        event_type="payment_reversed",
        description=f"Payment on invoice {invoice.invoice_number} reversed by the platform owner: {reason}",
        event_metadata={"invoice_number": invoice.invoice_number, "payment_id": str(target.id),
                        "reversal_id": str(reversal.id), "amount_ghs": str(target.amount_ghs),
                        "reversed_by": str(owner_id)},
    ))
    invoice.status = "issued"
    invoice.paid_at = None
    invoice.payment_reference = None
    await db.flush()
    return reversal, invoice


async def payments_by_invoice(db: AsyncSession, invoice_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict]]:
    """Every entry for these invoices, oldest first, with whether each payment
    has been reversed."""
    if not invoice_ids:
        return {}
    rows = (
        await db.execute(
            select(OperatorInvoicePayment)
            .where(OperatorInvoicePayment.invoice_id.in_(invoice_ids))
            .order_by(OperatorInvoicePayment.created_at, OperatorInvoicePayment.id)
        )
    ).scalars().all()
    reversed_ids = {r.reverses_payment_id for r in rows if r.kind == "reversal"}
    out: dict[uuid.UUID, list[dict]] = {}
    for r in rows:
        out.setdefault(r.invoice_id, []).append({
            "id": str(r.id),
            "kind": r.kind,
            "amount_ghs": str(r.amount_ghs),
            "method": r.method,
            "method_label": METHODS.get(r.method) if r.method else None,
            "reference": r.reference,
            "received_on": r.received_on.isoformat(),
            "note": r.note,
            "reverses_payment_id": str(r.reverses_payment_id) if r.reverses_payment_id else None,
            "reversed": r.kind == "payment" and r.id in reversed_ids,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return out


def receipt_for(invoice: OperatorInvoice, entries: list[dict] | None) -> dict | None:
    """How a paid invoice was paid, for the operator's receipt. None while unpaid."""
    if invoice.status != "paid":
        return None
    reference = invoice.payment_reference or ""
    if reference.startswith("manual:"):
        settled = next((e for e in (entries or []) if reference == f"manual:{e['id']}"), None)
        if settled:
            return {"method": settled["method_label"], "reference": settled["reference"],
                    "paid_on": settled["received_on"], "amount_ghs": settled["amount_ghs"]}
    return {"method": "Paystack (online)", "reference": reference or None,
            "paid_on": invoice.paid_at.date().isoformat() if invoice.paid_at else None,
            "amount_ghs": str(invoice.amount_ghs)}
