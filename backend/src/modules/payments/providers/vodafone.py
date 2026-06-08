import asyncio
import json
import logging
from decimal import Decimal
from typing import Optional

from src.config import Settings
from src.modules.payments.providers.base import PaymentProvider
from src.modules.payments.types import (
    PaymentInitiationResult,
    PaymentStatus,
    PaymentVerificationResult,
    PaymentWebhookResult,
)

logger = logging.getLogger("payments.providers.vodafone")


class VodafoneCashMockProvider(PaymentProvider):
    """
    Mock provider with realistic payload shape and async latency.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def initiate(
        self,
        amount_ghs: Decimal,
        phone: Optional[str],
        plan_id: str,
        site_id: str,
        internal_reference: str,
        payment_method: str,
    ) -> PaymentInitiationResult:
        await asyncio.sleep(2)
        mocked_payload = {
            "transaction_id": f"VOD-{internal_reference}",
            "merchant_id": self.settings.vodafone_cash_merchant_id or "mock-merchant",
            "customer_msisdn": phone,
            "amount": f"{amount_ghs:.2f}",
            "currency": "GHS",
            "reference": internal_reference,
            "status": "PENDING",
        }
        logger.info("Vodafone mock initiate: %s", mocked_payload)
        return PaymentInitiationResult(
            provider_reference=mocked_payload["transaction_id"],
            status=PaymentStatus.PENDING,
        )

    async def verify(self, provider_reference: str) -> PaymentVerificationResult:
        await asyncio.sleep(2)
        suffix = provider_reference[-1].lower()
        if suffix in {"0", "2", "4", "6", "8", "a", "c", "e"}:
            status = PaymentStatus.SUCCESS
            reason = None
        else:
            status = PaymentStatus.FAILED
            reason = "mock_declined"
        mocked_payload = {
            "transaction_id": provider_reference,
            "status": "SUCCESS" if status == PaymentStatus.SUCCESS else "FAILED",
            "reason": reason,
            "amount": "0.00",
        }
        logger.info("Vodafone mock verify: %s", mocked_payload)
        return PaymentVerificationResult(
            status=status,
            amount_ghs=Decimal("0.00"),
            provider_reference=provider_reference,
            failure_reason=reason,
        )

    async def handle_webhook(self, headers, raw_body: bytes) -> PaymentWebhookResult:
        payload = json.loads(raw_body.decode("utf-8"))
        tx_id = str(payload.get("transaction_id"))
        reference = str(payload.get("reference"))
        status_raw = str(payload.get("status", "PENDING")).upper()
        status = PaymentStatus.PENDING
        if status_raw == "SUCCESS":
            status = PaymentStatus.SUCCESS
        elif status_raw == "FAILED":
            status = PaymentStatus.FAILED
        return PaymentWebhookResult(
            internal_reference=reference,
            status=status,
            provider_reference=tx_id,
        )
