"""
Platform billing webhook — separate from per-operator webhooks.
Uses PLATFORM_BILLING_PAYSTACK_WEBHOOK_SECRET for signature verification.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.base import get_db
from src.db.models import ISPOperator, OperatorInvoice
from src.modules.billing.service import mark_invoice_paid, reactivate_operator
from src.modules.notifications import dispatcher as notify
from fastapi import Depends

logger = logging.getLogger("webhooks.platform_billing")
router = APIRouter(tags=["webhooks"])


def _verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/api/v1/webhooks/platform-billing/paystack")
async def platform_billing_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    raw_body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")

    if settings.platform_billing_paystack_webhook_secret:
        if not _verify_signature(raw_body, signature, settings.platform_billing_paystack_webhook_secret):
            raise HTTPException(401, "Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    event = payload.get("event")
    if event != "charge.success":
        return {"message": "Event ignored"}

    data = payload.get("data", {})
    metadata = data.get("metadata") or {}
    invoice_id = metadata.get("invoice_id")
    operator_id = metadata.get("operator_id")
    payment_reference = data.get("reference", "")

    if not invoice_id or not operator_id:
        logger.warning("platform_billing_webhook missing metadata invoice_id=%s operator_id=%s", invoice_id, operator_id)
        return {"message": "Missing metadata"}

    invoice = (
        await db.execute(select(OperatorInvoice).where(OperatorInvoice.id == invoice_id))
    ).scalar_one_or_none()

    if not invoice:
        logger.warning("platform_billing_webhook invoice_not_found id=%s", invoice_id)
        return {"message": "Invoice not found"}

    if invoice.status == "paid":
        return {"message": "Already paid"}

    operator = (
        await db.execute(select(ISPOperator).where(ISPOperator.id == operator_id))
    ).scalar_one_or_none()
    if not operator:
        return {"message": "Operator not found"}

    was_suspended = operator.status == "suspended"

    await mark_invoice_paid(db, invoice, payment_reference)

    if was_suspended:
        await reactivate_operator(db, operator)

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
        if was_suspended:
            await notify.notify_reactivated(
                email=operator.contact_email,
                phone=operator.contact_phone or "",
                isp_name=operator.name,
                next_invoice_date=next_invoice_date,
            )
    except Exception as exc:
        logger.error("platform_billing_webhook notify_error error=%s", exc)

    logger.info(
        "platform_billing_webhook_processed invoice=%s operator=%s reactivated=%s",
        invoice.invoice_number,
        operator.slug,
        was_suspended,
    )
    return {"message": "OK"}
