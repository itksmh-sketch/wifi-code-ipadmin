import json
import logging
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
) -> None:
    service = get_payment_service()
    async with async_session_factory() as db:
        tx_result = await db.execute(
            select(PaymentTransaction).where(PaymentTransaction.internal_reference == internal_reference)
        )
        tx = tx_result.scalar_one_or_none()
        if not tx:
            logger.warning("Webhook payment not found provider=%s reference=%s", provider, internal_reference)
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
