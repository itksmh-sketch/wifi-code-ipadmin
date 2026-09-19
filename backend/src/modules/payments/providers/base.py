from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Mapping, Optional

from src.modules.payments.types import (
    PROVIDER_UNREACHABLE_STATE,
    PaymentInitiationResult,
    PaymentNextAction,
    PaymentStatus,
    PaymentVerificationResult,
    PaymentWebhookResult,
)


def provider_unreachable_result(
    provider_reference: str,
    *,
    payment_channel: Optional[str] = None,
) -> PaymentVerificationResult:
    """The placeholder ``verify()`` returns when the provider could not be reached.

    A timeout or connection failure says nothing about the charge — it may well
    have succeeded. Reporting it as pending keeps the customer polling (webhooks
    and the reconciliation job resolve it either way) instead of surfacing a 500
    or, worse, a false failure.
    """
    return PaymentVerificationResult(
        status=PaymentStatus.PENDING,
        amount_ghs=Decimal("0.00"),
        provider_reference=provider_reference,
        next_action=PaymentNextAction.WAIT,
        provider_state=PROVIDER_UNREACHABLE_STATE,
        display_message="Still confirming your payment. This can take a minute.",
        payment_channel=payment_channel,
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
        client_ip: Optional[str] = None,
    ) -> PaymentInitiationResult:
        """Initiate a payment request at the provider.

        ``client_ip`` is the payer's IP when known; providers that don't need it
        (e.g. Paystack) ignore it.
        """

    @abstractmethod
    async def verify(
        self,
        provider_reference: str,
        expected_amount_ghs: Optional[Decimal] = None,
    ) -> PaymentVerificationResult:
        """Verify transaction status directly with the provider.

        When ``expected_amount_ghs`` is given, a provider that can see the
        settled amount should treat a materially smaller settlement (more than
        one pesewa short) as a failure rather than a success.
        """

    async def verify_credentials(self) -> str | None:
        """Check the stored credentials authenticate against the provider.

        Return on success; raise (any exception) on failure — the message is
        surfaced to the operator as the "test connection" result. May return a
        short detail string (e.g. an account balance) for the ``/test`` success
        message; ``None`` (the norm for card/MoMo gateways, which have no balance
        concept) means "verified, nothing extra to show".
        """
        raise NotImplementedError

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
