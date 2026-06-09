import hashlib
import hmac
import json
import logging
import re
from decimal import Decimal
from typing import Any, Optional

import httpx

from src.config import Settings
from src.modules.payments.providers.base import PaymentProvider
from src.modules.payments.providers.utils import redact_dict
from src.modules.payments.types import (
    PaymentInitiationResult,
    PaymentNextAction,
    PaymentStatus,
    PaymentVerificationResult,
    PaymentWebhookResult,
)

logger = logging.getLogger("payments.providers.paystack")


class PaystackProvider(PaymentProvider):
    _MOBILE_MONEY_PROVIDERS = {
        "mtn_momo": "mtn",
        "vodafone_cash": "vodafone",
        "airteltigo": "atl",
    }

    def __init__(
        self,
        settings: Settings | None = None,
        client: Optional[httpx.AsyncClient] = None,
        *,
        secret_key: str | None = None,
        public_key: str | None = None,
        webhook_secret: str | None = None,
        callback_url: str | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self.secret_key = secret_key if secret_key is not None else (settings.paystack_secret_key if settings else "")
        self.public_key = public_key if public_key is not None else (settings.paystack_public_key if settings else "")
        self.webhook_secret = webhook_secret if webhook_secret is not None else self.secret_key
        self.callback_url = callback_url if callback_url is not None else (settings.paystack_callback_url if settings else "")

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=15.0, base_url="https://api.paystack.co")

    async def initiate(
        self,
        amount_ghs: Decimal,
        phone: Optional[str],
        plan_id: str,
        site_id: str,
        internal_reference: str,
        payment_method: str,
    ) -> PaymentInitiationResult:
        if amount_ghs < Decimal("1.00"):
            raise ValueError("Amount too small for Paystack processing")

        amount_pesewas = int((amount_ghs * Decimal("100")).quantize(Decimal("1")))
        headers = {"Authorization": f"Bearer {self.secret_key}"}
        if not self.secret_key:
            raise ValueError("Paystack secret key is not configured")

        if payment_method == "card":
            return await self._initialize_card_payment(
                amount_pesewas=amount_pesewas,
                phone=phone,
                plan_id=plan_id,
                site_id=site_id,
                internal_reference=internal_reference,
                headers=headers,
            )

        provider_code = self._MOBILE_MONEY_PROVIDERS.get(payment_method)
        if not provider_code:
            return PaymentInitiationResult(
                provider_reference=internal_reference,
                status=PaymentStatus.FAILED,
                failure_reason="Unsupported mobile money provider",
                next_action=PaymentNextAction.NONE,
                provider_state="unsupported",
                display_message="Unsupported mobile money provider.",
            )
        if not phone:
            return PaymentInitiationResult(
                provider_reference=internal_reference,
                status=PaymentStatus.FAILED,
                failure_reason="Phone number is required",
                next_action=PaymentNextAction.NONE,
                provider_state="validation_failed",
                display_message="Please enter a phone number.",
                payment_channel="mobile_money",
            )

        body = {
            "email": self._paystack_email(phone=phone, internal_reference=internal_reference),
            "amount": amount_pesewas,
            "currency": "GHS",
            "reference": internal_reference,
            "mobile_money": {
                "phone": self._normalize_phone(phone),
                "provider": provider_code,
            },
        }
        client = await self._get_client()
        try:
            response = await client.post("/charge", headers=headers, json=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ValueError(self._paystack_error_message(exc)) from exc
        payload = response.json()
        logger.info("Paystack charge response: %s", redact_dict(payload if isinstance(payload, dict) else {}))
        return self._result_from_paystack_payload(payload, fallback_reference=internal_reference)

    async def verify(self, provider_reference: str) -> PaymentVerificationResult:
        if not self.secret_key:
            return PaymentVerificationResult(
                status=PaymentStatus.PENDING,
                amount_ghs=Decimal("0.00"),
                provider_reference=provider_reference,
                next_action=PaymentNextAction.WAIT,
                provider_state="pending",
                display_message="Awaiting payment confirmation.",
                payment_channel="mobile_money",
            )
        headers = {"Authorization": f"Bearer {self.secret_key}"}
        client = await self._get_client()
        try:
            response = await client.get(f"/charge/{provider_reference}", headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ValueError(self._paystack_error_message(exc)) from exc
        payload = response.json()
        logger.info("Paystack charge verify response: %s", redact_dict(payload if isinstance(payload, dict) else {}))
        return self._verification_from_paystack_payload(payload, fallback_reference=provider_reference)

    async def submit_otp(self, reference: str, otp: str) -> PaymentInitiationResult:
        if not self.secret_key:
            return PaymentInitiationResult(
                provider_reference=reference,
                status=PaymentStatus.PENDING,
                next_action=PaymentNextAction.WAIT,
                provider_state="pending",
                display_message="Check your phone and approve the mobile money payment prompt.",
                payment_channel="mobile_money",
            )
        return await self._submit_followup("/charge/submit_otp", {"otp": otp, "reference": reference}, reference)

    async def submit_phone(self, reference: str, phone: str) -> PaymentInitiationResult:
        return await self._submit_followup("/charge/submit_phone", {"phone": phone, "reference": reference}, reference)

    async def submit_pin(self, reference: str, pin: str) -> PaymentInitiationResult:
        return await self._submit_followup("/charge/submit_pin", {"pin": pin, "reference": reference}, reference)

    async def submit_birthday(self, reference: str, birthday: str) -> PaymentInitiationResult:
        return await self._submit_followup("/charge/submit_birthday", {"birthday": birthday, "reference": reference}, reference)

    async def submit_address(
        self,
        reference: str,
        *,
        address: str,
        city: str,
        state: str,
        zip_code: str,
    ) -> PaymentInitiationResult:
        return await self._submit_followup(
            "/charge/submit_address",
            {
                "address": address,
                "city": city,
                "state": state,
                "zip_code": zip_code,
                "reference": reference,
            },
            reference,
        )

    async def handle_webhook(self, headers, raw_body: bytes) -> PaymentWebhookResult:
        signature = headers.get("x-paystack-signature") or headers.get("X-Paystack-Signature")
        if not signature:
            raise ValueError("Missing paystack signature header")
        digest = hmac.new(
            self.webhook_secret.encode("utf-8"),
            raw_body,
            hashlib.sha512,
        ).hexdigest()
        if not hmac.compare_digest(signature, digest):
            raise ValueError("Invalid paystack signature")

        payload = json.loads(raw_body.decode("utf-8"))
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        reference = str(data.get("reference") or "")
        if not reference:
            raise ValueError("Webhook payload missing reference")
        event = str(payload.get("event", ""))
        status = PaymentStatus.PENDING
        provider_state = str(data.get("status") or event or "pending")
        display_message = "Awaiting payment confirmation."
        if event == "charge.success":
            status = PaymentStatus.SUCCESS
            display_message = "Payment successful."
        elif event in {"charge.failed", "charge.reversed"}:
            status = PaymentStatus.FAILED
            display_message = "Payment failed."
        return PaymentWebhookResult(
            internal_reference=reference,
            status=status,
            provider_reference=reference,
            provider_state=provider_state,
            display_message=display_message,
            provider_payload=payload if isinstance(payload, dict) else None,
            payment_channel=str(data.get("channel") or "mobile_money"),
        )

    async def _submit_followup(self, path: str, body: dict[str, Any], reference: str) -> PaymentInitiationResult:
        if not self.secret_key:
            return PaymentInitiationResult(
                provider_reference=reference,
                status=PaymentStatus.PENDING,
                next_action=PaymentNextAction.WAIT,
                provider_state="pending",
                display_message="Awaiting payment confirmation.",
                payment_channel="mobile_money",
            )
        headers = {"Authorization": f"Bearer {self.secret_key}"}
        client = await self._get_client()
        try:
            response = await client.post(path, headers=headers, json=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ValueError(self._paystack_error_message(exc)) from exc
        payload = response.json()
        logger.info("Paystack follow-up response path=%s body=%s", path, redact_dict(payload if isinstance(payload, dict) else {}))
        return self._result_from_paystack_payload(payload, fallback_reference=reference)

    async def _initialize_card_payment(
        self,
        *,
        amount_pesewas: int,
        phone: Optional[str],
        plan_id: str,
        site_id: str,
        internal_reference: str,
        headers: dict[str, str],
    ) -> PaymentInitiationResult:
        body: dict[str, Any] = {
            "email": self._paystack_email(phone=phone, internal_reference=internal_reference),
            "amount": amount_pesewas,
            "currency": "GHS",
            "reference": internal_reference,
            "metadata": {
                "plan_id": plan_id,
                "site_id": site_id,
                "payment_method": "card",
            },
        }
        if self.callback_url:
            body["callback_url"] = self.callback_url

        client = await self._get_client()
        try:
            response = await client.post("/transaction/initialize", headers=headers, json=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ValueError(self._paystack_error_message(exc)) from exc
        payload = response.json()
        logger.info("Paystack transaction initialize response: %s", redact_dict(payload if isinstance(payload, dict) else {}))
        return self._result_from_paystack_payload(payload, fallback_reference=internal_reference, payment_channel="card")

    def _result_from_paystack_payload(
        self,
        payload: dict[str, Any],
        *,
        fallback_reference: str,
        payment_channel: str | None = None,
    ) -> PaymentInitiationResult:
        parsed = self._parse_paystack_payload(
            payload,
            fallback_reference=fallback_reference,
            payment_channel=payment_channel,
        )
        return PaymentInitiationResult(**parsed)

    def _verification_from_paystack_payload(
        self,
        payload: dict[str, Any],
        *,
        fallback_reference: str,
    ) -> PaymentVerificationResult:
        parsed = self._parse_paystack_payload(payload, fallback_reference=fallback_reference)
        amount_ghs = Decimal(str(parsed["provider_payload"].get("amount", 0))) / Decimal("100")
        return PaymentVerificationResult(
            status=parsed["status"],
            amount_ghs=amount_ghs.quantize(Decimal("0.01")),
            provider_reference=parsed["provider_reference"],
            failure_reason=parsed["failure_reason"],
            next_action=parsed["next_action"],
            provider_state=parsed["provider_state"],
            display_message=parsed["display_message"],
            provider_payload=parsed["provider_payload"],
            payment_channel=parsed["payment_channel"],
        )

    def _parse_paystack_payload(
        self,
        payload: dict[str, Any],
        *,
        fallback_reference: str,
        payment_channel: str | None = None,
    ) -> dict[str, Any]:
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        status_raw = str(data.get("status") or payload.get("status") or "pending").lower()
        provider_reference = str(data.get("reference") or fallback_reference)
        message = str(data.get("display_text") or payload.get("message") or "").strip() or None
        channel = str(payment_channel or data.get("channel") or "mobile_money")

        status = PaymentStatus.PENDING
        next_action = PaymentNextAction.WAIT
        failure_reason = None
        authorization_url = None

        if status_raw == "success":
            status = PaymentStatus.SUCCESS
            next_action = PaymentNextAction.NONE
            message = message or "Payment successful."
        elif status_raw in {"failed", "abandoned", "reversed"}:
            status = PaymentStatus.FAILED
            next_action = PaymentNextAction.NONE
            failure_reason = str(data.get("gateway_response") or data.get("message") or "payment failed")
            message = message or "Payment failed."
        elif status_raw in {"send_otp", "pay_offline"}:
            next_action = PaymentNextAction.WAIT
            message = message or "Check your phone and approve the mobile money payment prompt."
        elif status_raw == "send_phone":
            next_action = PaymentNextAction.ENTER_PHONE
            message = message or "Enter the requested phone number."
        elif status_raw == "send_pin":
            next_action = PaymentNextAction.ENTER_PIN
            message = message or "Enter your card or wallet PIN."
        elif status_raw == "send_birthday":
            next_action = PaymentNextAction.ENTER_BIRTHDAY
            message = message or "Enter your date of birth."
        elif status_raw == "send_address":
            next_action = PaymentNextAction.ENTER_ADDRESS
            message = message or "Enter your billing address."
        elif status_raw == "open_url":
            next_action = PaymentNextAction.OPEN_URL
            authorization_url = str(data.get("authorization_url") or data.get("url") or "")
            message = message or "Additional authorization is required."
        elif data.get("authorization_url"):
            next_action = PaymentNextAction.OPEN_URL
            authorization_url = str(data.get("authorization_url") or "")
            message = message or "Continue to Paystack to complete your card payment."
        else:
            next_action = PaymentNextAction.WAIT
            message = message or "Check your phone and complete the payment prompt."

        return {
            "provider_reference": provider_reference,
            "status": status,
            "failure_reason": failure_reason,
            "next_action": next_action,
            "provider_state": status_raw,
            "display_message": message,
            "provider_payload": data if isinstance(data, dict) else payload,
            "payment_channel": channel,
            "authorization_url": authorization_url,
        }

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        return re.sub(r"\D", "", phone)

    @classmethod
    def _paystack_email(cls, *, phone: Optional[str], internal_reference: str) -> str:
        if phone:
            phone_clean = cls._normalize_phone(phone)
            if phone_clean:
                return f"{phone_clean}@hotspot.yourisp.com"
        return f"pay_{internal_reference[:12]}@hotspot.yourisp.com"

    @staticmethod
    def _paystack_error_message(exc: httpx.HTTPStatusError) -> str:
        try:
            payload = exc.response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error")
            if message:
                return str(message)
        return exc.response.text or "Paystack request failed"
