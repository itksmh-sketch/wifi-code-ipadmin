"""
Platform billing webhook — separate from per-operator webhooks.
Requires PLATFORM_BILLING_PAYSTACK_WEBHOOK_SECRET; requests are refused if it is unset.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.base import get_db
from src.db.models import ISPOperator, OperatorBillingEvent, OperatorInvoice
from src.modules.billing.service import (
    ghs_to_pesewas,
    is_billing_suspension,
    mark_invoice_paid,
    normalise_billing_status,
    reactivate_operator,
)
from src.modules.platform.payment_credentials_service import resolve_paystack_keys
from src.modules.webhooks.charge_verification import ChargeRejection, verify_charge
from src.modules.notifications import dispatcher as notify
from fastapi import Depends

logger = logging.getLogger("webhooks.platform_billing")
router = APIRouter(tags=["webhooks"])


def _verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _find_invoice(db: AsyncSession, payload: dict, reference: str) -> OperatorInvoice | None:
    """Locate the invoice a charge settles, by three routes.

    Metadata alone used to be the only route, so a payload whose metadata was
    stripped or malformed returned 200 and the payment was silently lost.
    """
    metadata = (payload.get("data") or {}).get("metadata") or {}

    # 1. Metadata, as set by initiate_invoice_payment.
    raw_id = metadata.get("invoice_id") if isinstance(metadata, dict) else None
    if raw_id:
        try:
            found = (
                await db.execute(select(OperatorInvoice).where(OperatorInvoice.id == uuid.UUID(str(raw_id))))
            ).scalar_one_or_none()
        except (ValueError, AttributeError, TypeError):
            found = None
        if found is not None:
            return found

    # 2. The reference format initiate_invoice_payment writes: INV-{invoice uuid}.
    if reference.startswith("INV-"):
        try:
            found = (
                await db.execute(
                    select(OperatorInvoice).where(OperatorInvoice.id == uuid.UUID(reference[4:]))
                )
            ).scalar_one_or_none()
        except (ValueError, AttributeError, TypeError):
            found = None
        if found is not None:
            return found

    # 3. A reference already recorded against an invoice (replays, reconciliation).
    if reference:
        return (
            await db.execute(
                select(OperatorInvoice).where(OperatorInvoice.payment_reference == reference)
            )
        ).scalar_one_or_none()
    return None


@router.post("/api/v1/webhooks/platform-billing/paystack")
async def platform_billing_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    raw_body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")

    # Paystack signs webhooks with the SECRET KEY, not a separate credential, so
    # an explicitly stored webhook secret is treated as an override and the
    # secret key is the default. Resolved through the credentials service, so a
    # key stored in platform_payment_credentials is used without also having to
    # duplicate it into .env (which remains the fallback).
    keys = await resolve_paystack_keys(db)
    webhook_secret = keys.webhook_secret or keys.secret_key
    if not webhook_secret:
        # Fail closed: with no configured secret we cannot verify anything, so the
        # payload must be refused rather than trusted. Paystack retries non-2xx,
        # so deliveries are not lost once the secret is configured.
        logger.error("platform_billing_webhook rejected reason=webhook_secret_not_configured")
        raise HTTPException(403, "Platform billing webhook secret is not configured")
    if not _verify_signature(raw_body, signature, webhook_secret):
        raise HTTPException(401, "Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    reference = str((payload.get("data") or {}).get("reference") or "")

    invoice = await _find_invoice(db, payload, reference)
    if invoice is None:
        # 200: a payload we cannot match will never match on retry either.
        logger.warning("platform_billing_webhook invoice_not_found reference=%s", reference)
        return {"message": "Invoice not found"}

    if invoice.status == "paid":
        return {"message": "Already paid"}

    # The invoice knows who it belongs to. Taking the operator from the payload
    # would let a charge assert a different tenant than the one being billed.
    operator = (
        await db.execute(select(ISPOperator).where(ISPOperator.id == invoice.isp_operator_id))
    ).scalar_one_or_none()
    if not operator:
        logger.error("platform_billing_webhook operator_missing invoice=%s", invoice.invoice_number)
        return {"message": "Operator not found"}

    verdict = verify_charge(payload, expected_pesewas=ghs_to_pesewas(invoice.amount_ghs))

    if not verdict.accepted:
        if verdict.rejection is ChargeRejection.NOT_SUCCESS:
            return {"message": "Event ignored"}

        # Money may well have moved; it just does not settle this invoice. Record
        # it so a human sees it, and return 200 — the same payload would fail
        # identically on every retry, so retrying only generates noise.
        logger.error(
            "platform_billing_webhook_rejected invoice=%s operator=%s reason=%s detail=%s",
            invoice.invoice_number, operator.slug, verdict.rejection.value, verdict.reason,
        )
        db.add(OperatorBillingEvent(
            isp_operator_id=operator.id,
            event_type="payment_rejected",
            description=(
                f"Payment for invoice {invoice.invoice_number} rejected "
                f"({verdict.rejection.value}): {verdict.reason}"
            ),
            event_metadata={
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "payment_reference": reference,
                "rejection": verdict.rejection.value,
                "reason": verdict.reason,
                "charged_pesewas": verdict.charged_pesewas,
                "expected_pesewas": verdict.expected_pesewas,
                "currency": verdict.currency,
            },
        ))
        await db.commit()
        return {"message": f"Payment rejected: {verdict.rejection.value}"}

    if verdict.overpaid:
        logger.warning(
            "platform_billing_webhook_overpaid invoice=%s operator=%s charged_ghs=%s invoice_ghs=%s suspicious=%s",
            invoice.invoice_number, operator.slug, verdict.charged_ghs, invoice.amount_ghs,
            verdict.overpaid_suspiciously,
        )

    # Only a billing suspension may be lifted by paying. A suspension the
    # platform owner imposed — or one whose cause was never recorded — stands.
    was_billing_suspended = is_billing_suspension(operator)

    await mark_invoice_paid(db, invoice, reference, amount_charged_ghs=verdict.charged_ghs)
    # Independent of suspension: an operator who paid inside the grace period is
    # past_due but not suspended, and would otherwise stay past_due forever and
    # never be invoiced again.
    await normalise_billing_status(db, operator)

    if was_billing_suspended:
        await reactivate_operator(db, operator)
    elif operator.status == "suspended":
        logger.warning(
            "platform_billing_webhook_not_reactivated invoice=%s operator=%s reason=%s "
            "(payment recorded; suspension was not billing-caused)",
            invoice.invoice_number, operator.slug, operator.suspension_reason,
        )

    await db.commit()
    await db.refresh(operator)

    # Calculate next invoice date for notification
    from calendar import monthrange
    now = datetime.now(timezone.utc)
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1)
    else:
        next_month = now.replace(month=now.month + 1, day=1)
    next_invoice_date = next_month.strftime("%B %d, %Y")

    try:
        if was_billing_suspended:
            await notify.notify_reactivated(
                email=operator.contact_email,
                phone=operator.contact_phone or "",
                isp_name=operator.name,
                next_invoice_date=next_invoice_date,
            )
    except Exception as exc:
        logger.error("platform_billing_webhook notify_error error=%s", exc)

    was_suspended = was_billing_suspended
    logger.info(
        "platform_billing_webhook_processed invoice=%s operator=%s reactivated=%s",
        invoice.invoice_number,
        operator.slug,
        was_suspended,
    )
    return {"message": "OK"}
