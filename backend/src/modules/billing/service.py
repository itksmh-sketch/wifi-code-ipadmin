from __future__ import annotations
import secrets
import uuid
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import httpx
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.modules import audit
from src.modules.audit.fields import OPERATOR_STATUS
from src.db.models import (
    ISPOperator,
    OperatorInvoice,
    OperatorInvoiceLineItem,
    OperatorBillingEvent,
    PlatformSetting,
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

# platform_settings key holding the platform-wide default monthly fee. Read by
# both onboarding paths (create_operator, approve_application) to stamp a fee on
# a new operator; a new operator never carries a client-supplied fee.
DEFAULT_MONTHLY_FEE_KEY = "default_monthly_fee_ghs"
# Last-resort value if every configured source is missing or unusable. Kept in
# step with config.default_monthly_fee_ghs.
_DEFAULT_MONTHLY_FEE_FALLBACK = Decimal("200.00")


def _coerce_payable_fee(raw) -> Decimal | None:
    """Parse `raw` to a fee that satisfies the isp_operators CHECK
    (``monthly_fee_ghs = 0 OR monthly_fee_ghs >= 1.00``).

    Returns None — not a raised error — when `raw` is absent, unparseable,
    negative, or lands in the unpayable band ``(0, 1.00)``, so the caller can
    fall through to the next source. This is the guarantee that a stray value in
    platform_settings or a bad DEFAULT_MONTHLY_FEE_GHS in the environment can
    never reach ``ISPOperator(monthly_fee_ghs=...)`` and trip the constraint.
    """
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite() or value < 0:
        return None
    if Decimal(0) < value < PAYSTACK_MINIMUM_GHS:
        return None
    # Numeric(10, 2) — validation above already guarantees the result stays in
    # {0} ∪ [1.00, ∞), so rounding to 2dp cannot push it into the banned band.
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def get_default_monthly_fee(db: AsyncSession) -> Decimal:
    """The fee a newly-created operator is stamped with.

    Resolution order — platform_settings row, then config/.env, then the
    hard-coded fallback — with every candidate passed through
    :func:`_coerce_payable_fee`. The return value is therefore *always* either
    ``Decimal("0.00")`` or ``>= Decimal("1.00")``: operator creation can never
    fail with a raw CHECK-constraint error.
    """
    stored = (
        await db.execute(
            select(PlatformSetting.value).where(PlatformSetting.key == DEFAULT_MONTHLY_FEE_KEY)
        )
    ).scalar_one_or_none()
    resolved = _coerce_payable_fee(stored)
    if resolved is not None:
        return resolved
    resolved = _coerce_payable_fee(get_settings().default_monthly_fee_ghs)
    if resolved is not None:
        return resolved
    return _DEFAULT_MONTHLY_FEE_FALLBACK


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
    amount_ghs: Decimal | None = None,
    metadata: dict | None = None,
    sort_order: int = 0,
) -> OperatorInvoiceLineItem:
    """Add a charge to an invoice and recompute its total.

    THE ONLY SUPPORTED WAY TO ADD A LINE. It recomputes `invoice.amount_ghs` in
    the same flush, so the stored total can never drift from what the lines sum
    to. Inserting an OperatorInvoiceLineItem directly, or assigning
    `invoice.amount_ghs` by hand, breaks that guarantee — don't.

    `amount_ghs` is normally omitted: the stored amount is quantity *
    unit_price_ghs, rounded once (line_amount). Pass it explicitly only when
    the line represents a set of charges that were each already rounded
    individually before this call (e.g. the SMS usage rollup, where every
    usage record was rounded to the cent at metering time) — quantity and
    unit_price_ghs are still stored for display, but the recorded amount is
    the caller's exact sum, which can differ by a cent from a fresh
    quantity*unit_price recomputation at a different rounding granularity.
    The invoice-total guarantee is unaffected either way: recompute_invoice_total
    sums whatever amount_ghs ends up on each line, not how it was derived.

    Caller commits.
    """
    line = OperatorInvoiceLineItem(
        invoice_id=invoice.id,
        kind=kind,
        description=description,
        quantity=Decimal(quantity),
        unit_price_ghs=Decimal(unit_price_ghs),
        amount_ghs=Decimal(amount_ghs) if amount_ghs is not None else line_amount(quantity, unit_price_ghs),
        line_metadata=metadata,
        sort_order=sort_order,
    )
    db.add(line)
    await db.flush()
    await recompute_invoice_total(db, invoice)
    return line


def record_invoice_issued(db: AsyncSession, invoice: OperatorInvoice, *, via: str) -> None:
    """The audit row and billing event for an invoice the billing jobs issued,
    once its lines are all on it (the monthly run adds SMS usage after the
    subscription line), so both carry the final total. By the system; in the
    operator's trail. Caller commits."""
    db.add(OperatorBillingEvent(
        isp_operator_id=invoice.isp_operator_id,
        event_type="invoice_issued",
        description=f"Invoice {invoice.invoice_number} issued for GHS {invoice.amount_ghs}.",
        event_metadata={"invoice_number": invoice.invoice_number, "amount_ghs": str(invoice.amount_ghs)},
    ))
    audit.record_system(
        db, "invoice.issue", isp_operator_id=invoice.isp_operator_id, target_type="operator_invoice",
        target_id=invoice.id, summary=f"Issued invoice {invoice.invoice_number} for GHS {invoice.amount_ghs}",
        detail={"invoice_number": invoice.invoice_number, "amount_ghs": invoice.amount_ghs, "via": via,
                "period_start": invoice.period_start, "period_end": invoice.period_end, "due_at": invoice.due_at},
    )


def _record(db: AsyncSession, by_system: bool, action: str, **kwargs) -> None:
    """A webhook's or job's row is the system's; a payment the platform owner
    records by hand is theirs (the request's actor)."""
    if by_system:
        audit.record_system(db, action, **kwargs)
    else:
        audit.record(db, action, **kwargs)


def _operator_status_row(db: AsyncSession, operator: ISPOperator, before: dict, action: str, summary: str,
                         detail: dict | None = None, *, by_system: bool = True) -> None:
    changes = OPERATOR_STATUS.diff(before, OPERATOR_STATUS.snapshot(operator))
    if changes:
        _record(db, by_system, action, isp_operator_id=operator.id, target_type="isp_operator",
                target_id=operator.id, summary=summary, changes=changes, detail=detail)


def first_invoice_date(trial_ends_at: datetime) -> date:
    """The day monthly billing starts after a trial: the 1st of the month after
    the one the trial ends in. Never the trial's own month, so an operator is
    never invoiced at trial end and then again on the 1st."""
    day = trial_ends_at.astimezone(timezone.utc).date()
    return date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)


def trial_covers_period(operator: ISPOperator, period_start: date) -> bool:
    """Whether the month starting period_start is still before billing starts
    for this operator (their trial ran into it). An operator who never had a
    trial is billed from their first month."""
    return operator.trial_ends_at is not None and first_invoice_date(operator.trial_ends_at) > period_start


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

    # An operator on a GHS 0 fee is invoiced only for SMS usage: no
    # subscription line at all rather than one for nothing.
    if Decimal(operator.monthly_fee_ghs) > 0:
        await add_line_item(
            db,
            invoice,
            kind="subscription",
            description=f"Monthly subscription — {period_start:%d %b %Y} to {period_end:%d %b %Y}",
            quantity=Decimal("1"),
            unit_price_ghs=Decimal(operator.monthly_fee_ghs),
            sort_order=0,
        )
    # The invoice_issued event is written by record_invoice_issued, once every
    # line is on, so it can never disagree with the invoice's total.
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


# Paystack states in which a checkout already opened for an invoice can still be
# completed. Reusing it, rather than opening another, means an operator can
# never have two live checkouts for one invoice (and so pay it twice).
_REUSABLE_PAYSTACK_STATES = frozenset({"abandoned", "ongoing", "pending", "processing", "queued"})


async def _existing_checkout_state(client: httpx.AsyncClient, secret_key: str, reference: str,
                                   expected_pesewas: int) -> str:
    """What to do with the checkout already stored on an invoice.

    "reuse": it can still be paid (or Paystack could not be asked: reusing
    never opens a second charge). "paid": the money went through and the
    webhook has not landed yet. "new": it is finished without paying, for a
    different amount, or unknown to these keys (opened under test keys).
    """
    try:
        resp = await client.get(f"/transaction/verify/{reference}",
                                headers={"Authorization": f"Bearer {secret_key}"})
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return "reuse"
    if resp.status_code == 401:
        raise ValueError(f"Paystack error: {data.get('message', 'unauthorized')}")
    if resp.status_code >= 500:
        return "reuse"
    if not data.get("status"):
        return "new"
    tx = data.get("data") or {}
    state = str(tx.get("status") or "").lower()
    if state == "success":
        return "paid"
    if state in _REUSABLE_PAYSTACK_STATES and tx.get("amount") == expected_pesewas:
        return "reuse"
    return "new"


async def initiate_invoice_payment(
    db: AsyncSession,
    invoice: OperatorInvoice,
    operator: ISPOperator,
) -> str:
    """Returns the Paystack authorization_url.

    Paystack refuses a reference it has seen before, so the first checkout
    for an invoice uses INV-{id} and is stored with its reference; a later
    attempt returns that same checkout while it can still be paid, and only
    opens a new one (INV-{id}-{suffix}) once it cannot.
    """
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
    # /api/v1 prefix included: the billing router is mounted under it, so the
    # bare "/billing/payment-callback" this used to send returned 404 and every
    # payer landed on an error page after paying.
    app_url = (await get_setting(db, "platform_app_url")).rstrip("/")
    callback_url = f"{app_url}/api/v1/billing/payment-callback"

    async with httpx.AsyncClient(timeout=15, base_url="https://api.paystack.co") as client:
        reference = f"INV-{invoice.id}"
        if invoice.paystack_payment_url:
            # A checkout stored before references were kept used INV-{id}.
            previous = invoice.payment_reference or reference
            state = await _existing_checkout_state(client, keys.secret_key, previous, amount_pesewas)
            if state == "reuse":
                return invoice.paystack_payment_url
            if state == "paid":
                raise ValueError(
                    f"A payment for invoice {invoice.invoice_number} has gone through and is being "
                    "confirmed. Refresh this page in a minute."
                )
            reference = f"INV-{invoice.id}-{secrets.token_hex(4)}"

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
        resp = await client.post(
            "/transaction/initialize",
            json=payload,
            headers={"Authorization": f"Bearer {keys.secret_key}"},
        )

    data = resp.json()
    if not data.get("status"):
        raise ValueError(f"Paystack error: {data.get('message', 'unknown')}")

    auth_url: str = data["data"]["authorization_url"]

    # The checkout and its reference, so the next attempt can find it. Until
    # the invoice is paid, payment_reference is the open checkout's reference;
    # mark_invoice_paid then records the one that settled it.
    invoice.paystack_payment_url = auth_url
    invoice.payment_reference = reference
    await db.commit()

    return auth_url


async def mark_invoice_paid(
    db: AsyncSession,
    invoice: OperatorInvoice,
    payment_reference: str,
    amount_charged_ghs: Decimal | None = None,
    *,
    paid_at: datetime | None = None,
    detail: dict | None = None,
    by_system: bool = True,
) -> OperatorInvoice:
    """`paid_at` defaults to now (a Paystack charge settles as it lands); a
    payment recorded by hand passes the day the money came in. `detail` adds
    to the audit row and the event (how a manual payment came in)."""
    now = datetime.now(timezone.utc)
    _record(
        db, by_system, "invoice.paid", isp_operator_id=invoice.isp_operator_id, target_type="operator_invoice",
        target_id=invoice.id, summary=f"Invoice {invoice.invoice_number} paid",
        changes={"status": {"before": invoice.status, "after": "paid"}},
        detail={"invoice_number": invoice.invoice_number, "payment_reference": payment_reference,
                "invoice_amount_ghs": invoice.amount_ghs, "amount_charged_ghs": amount_charged_ghs, **(detail or {})},
    )
    invoice.status = "paid"
    invoice.paid_at = paid_at or now
    invoice.payment_reference = payment_reference

    # What was actually charged is recorded even when it exceeds the invoice.
    # Overpayment is accepted rather than credited — there is no credit balance —
    # so the event metadata is the only record that the excess happened.
    metadata = {"invoice_number": invoice.invoice_number, "payment_reference": payment_reference,
                **{k: str(v) for k, v in (detail or {}).items() if v is not None}}
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


async def normalise_billing_status(db: AsyncSession, operator: ISPOperator, *, by_system: bool = True) -> bool:
    """Put a paid-up operator back to billing_status 'active'. Caller commits.

    Runs on *any* successful payment, independent of suspension. Previously this
    only happened as a side effect of reactivation, so an operator who paid
    inside the grace period — past_due but not yet suspended — stayed past_due
    forever, and generate_monthly_invoices (which selects billing_status ==
    'active') silently never invoiced them again.

    Never for a cancelled operator (paying a final invoice must not restart
    their invoicing) or a paused one (a manual suspension pauses invoicing
    until the platform lifts it): those only change through the platform's
    status route.

    Returns True if it changed anything.
    """
    if operator.billing_status in ("active", "cancelled", "paused"):
        return False
    before = OPERATOR_STATUS.snapshot(operator)
    operator.billing_status = "active"
    _operator_status_row(db, operator, before, "operator.billing_status_change",
                         f"{operator.name} is paid up: billing back to active", by_system=by_system)
    await db.flush()
    return True


def resumed_billing_status(operator: ISPOperator, now: datetime | None = None) -> str:
    """The billing status an operator goes back to when the platform lifts a
    manual suspension or restores a cancelled operator: still on trial if the
    trial has not ended, otherwise invoiced as usual."""
    now = now or datetime.now(timezone.utc)
    if operator.trial_ends_at is not None and operator.trial_ends_at > now:
        return "trial"
    return "active"


def is_billing_suspension(operator: ISPOperator) -> bool:
    """Whether a payment is allowed to lift this operator's suspension.

    Only a suspension raised by the billing system. NULL means we do not know
    why they are suspended, and unknown is treated as 'no' — wrongly reactivating
    an operator suspended for abuse is silent and bad, whereas leaving a paid-up
    operator suspended is visible and manually recoverable.
    """
    return operator.status == "suspended" and operator.suspension_reason == "billing"


async def reactivate_operator(db: AsyncSession, operator: ISPOperator, *, by_system: bool = True) -> ISPOperator:
    """Lift a *billing* suspension. Callers must gate on is_billing_suspension()."""
    before = OPERATOR_STATUS.snapshot(operator)
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
    _operator_status_row(db, operator, before, "operator.reactivate",
                         f"{operator.name} reactivated: the overdue invoice was paid", detail={"reason": "billing"},
                         by_system=by_system)
    await db.flush()
    return operator


async def settle_invoice(
    db: AsyncSession,
    invoice: OperatorInvoice,
    operator: ISPOperator,
    *,
    payment_reference: str,
    amount_charged_ghs: Decimal | None = None,
    paid_at: datetime | None = None,
    detail: dict | None = None,
    by_system: bool = True,
) -> bool:
    """Everything a payment does, whichever way it came in (the Paystack
    webhook, or a payment the platform owner recorded by hand): the invoice
    is paid, billing goes back to active, and a *billing* suspension is lifted.
    The caller holds the operator's row lock and commits.

    Returns whether a billing suspension was lifted (the caller sends the
    reactivation notice after committing)."""
    # Only a billing suspension may be lifted by paying. A suspension the
    # platform owner imposed — or one whose cause was never recorded — stands.
    was_billing_suspended = is_billing_suspension(operator)
    await mark_invoice_paid(db, invoice, payment_reference, amount_charged_ghs=amount_charged_ghs,
                            paid_at=paid_at, detail=detail, by_system=by_system)
    # Independent of suspension: an operator who paid inside the grace period is
    # past_due but not suspended, and would otherwise stay past_due forever and
    # never be invoiced again. (A cancelled or paused operator is left alone.)
    await normalise_billing_status(db, operator, by_system=by_system)
    if was_billing_suspended:
        await reactivate_operator(db, operator, by_system=by_system)
    return was_billing_suspended
