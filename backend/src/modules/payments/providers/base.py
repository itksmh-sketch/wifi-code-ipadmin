from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Mapping, Optional

from src.modules.payments.types import (
    PaymentInitiationResult,
    PaymentVerificationResult,
    PaymentWebhookResult,
)


class PaymentProvider(ABC):
    """
    Provider contract for payment integrations.

    New providers should implement this interface only. The business
    orchestration layer (PaymentService) should not need changes.
    """

    @abstractmethod
    async def initiate(
        self,
        amount_ghs: Decimal,
        phone: Optional[str],
        plan_id: str,
        site_id: str,
        internal_reference: str,
        payment_method: str,
    ) -> PaymentInitiationResult:
        """Initiate a payment request at the provider."""

    @abstractmethod
    async def verify(self, provider_reference: str) -> PaymentVerificationResult:
        """Verify transaction status directly with the provider."""

    async def submit_otp(self, reference: str, otp: str) -> PaymentInitiationResult:
        raise NotImplementedError

    async def submit_phone(self, reference: str, phone: str) -> PaymentInitiationResult:
        raise NotImplementedError

    async def submit_pin(self, reference: str, pin: str) -> PaymentInitiationResult:
        raise NotImplementedError

    async def submit_birthday(self, reference: str, birthday: str) -> PaymentInitiationResult:
        raise NotImplementedError

    async def submit_address(
        self,
        reference: str,
        *,
        address: str,
        city: str,
        state: str,
        zip_code: str,
    ) -> PaymentInitiationResult:
        raise NotImplementedError

    @abstractmethod
    async def handle_webhook(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> PaymentWebhookResult:
        """
        Verify and parse webhook payload.

        Implementations must reject unverifiable payloads and only return
        validated status data.
        """
