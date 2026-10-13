import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from src.db.base import async_session_factory
from src.db.models import PaymentTransaction
from src.modules.payments.dependencies import get_payment_service
from src.modules.payments.types import PaymentStatus

logger = logging.getLogger(__name__)


async def process_webhook_event(
    ctx,
    provider: str,
    internal_reference: str,
    status: str,
    provider_reference: str | None,
    raw_payload: str,
    isp_operator_id: str | None = None,
) -> None:
    """``isp_operator_id`` is the operator whose key verified the webhook. The
    payment must be that operator's: each operator holds its own signing key,
    so without this one operator could sign a webhook naming another's payment.

    A job without it is dropped, never processed by reference alone. Every job
    has carried it since 2026-10-09; one without it can only be a leftover or a
    job enqueued by something other than the webhook route. The route has
    already answered 200, so the provider won't resend it; the payment stays
    pending and the reconciliation job (every 10 minutes, once a payment is 15
    minutes old) settles it with the provider, as for a webhook that never
    arrives."""
    if not isp_operator_id:
        logger.error("Webhook job dropped: no operator id provider=%s reference=%s", provider, internal_reference)
        return
    service = get_payment_service()
    async with async_session_factory() as db:
        stmt = select(PaymentTransaction).where(
            PaymentTransaction.internal_reference == internal_reference,
            PaymentTransaction.isp_operator_id == uuid.UUID(str(isp_operator_id)),
        )
        tx = (await db.execute(stmt)).scalar_one_or_none()
        if not tx:
            # The same for an unknown reference and another operator's payment.
            logger.warning("Webhook payment not found provider=%s reference=%s operator=%s",
                           provider, internal_reference, isp_operator_id)
            return

        tx.webhook_payload = json.loads(raw_payload)
        if provider_reference:
            tx.provider_reference = provider_reference
        # Commit webhook payload/reference update first so that downstream
        # success resolution can run in a clean transaction context.
        await db.commit()

        if provider == "flutterwave":
            # Flutterwave's verif-hash is a static shared secret, weaker than
            # Paystack's HMAC. Never trust the webhook's own status field — use it
            # only as a trigger to re-verify the transaction server-to-server.
            await service.refresh_transaction_status(db, tx=tx, force=True)
            return

        await service.apply_webhook_update(
            db,
            tx=tx,
            status=PaymentStatus(status),
            provider_reference=provider_reference,
            provider_state=None,
            display_message=None,
            provider_payload=json.loads(raw_payload),
            payment_channel=None,
            trigger_source="webhook",
        )
