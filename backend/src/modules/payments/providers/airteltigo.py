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

logger = logging.getLogger("payments.providers.airteltigo")


class AirtelTigoMockProvider(PaymentProvider):
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
            "request_id": f"AT-{internal_reference}",
            "msisdn": phone,
            "amount": f"{amount_ghs:.2f}",
            "currency": "GHS",
            "reference": internal_reference,
            "description": f"Plan {plan_id}",
            "status": "PENDING",
        }
        logger.info("AirtelTigo mock initiate: %s", mocked_payload)
        return PaymentInitiationResult(
            provider_reference=mocked_payload["request_id"],
            status=PaymentStatus.PENDING,
        )

    async def verify(self, provider_reference: str) -> PaymentVerificationResult:
        await asyncio.sleep(2)
        suffix = provider_reference[-1].lower()
        if suffix in {"1", "3", "5", "7", "9", "b", "d", "f"}:
            status = PaymentStatus.SUCCESS
            reason = None
        else:
            status = PaymentStatus.FAILED
            reason = "mock_timeout_or_decline"
        mocked_payload = {
            "request_id": provider_reference,
            "status": "SUCCESS" if status == PaymentStatus.SUCCESS else "FAILED",
            "reason": reason,
            "amount": "0.00",
        }
        logger.info("AirtelTigo mock verify: %s", mocked_payload)
        return PaymentVerificationResult(
            status=status,
            amount_ghs=Decimal("0.00"),
            provider_reference=provider_reference,
            failure_reason=reason,
        )

    async def handle_webhook(self, headers, raw_body: bytes) -> PaymentWebhookResult:
        payload = json.loads(raw_body.decode("utf-8"))
        request_id = str(payload.get("request_id"))
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
            provider_reference=request_id,
        )
