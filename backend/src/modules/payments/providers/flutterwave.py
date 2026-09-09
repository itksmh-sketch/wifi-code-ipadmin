"""Flutterwave v3 payment provider — bring-your-own, operator-supplied keys.

Shape notes vs PaystackProvider (the reference implementation):

  * Amounts are **major units** (GHS 5.00 -> "5.00"), not subunits/pesewas.
  * One identifier throughout: our `internal_reference` is sent as `tx_ref` and
    is what `verify` and the webhook key on. Flutterwave's own `flw_ref` is only
    needed for OTP validation and is resolved on demand.
  * Card goes through **hosted Standard checkout** (`POST /v3/payments` -> a
    redirect link). No direct card charge, no 3DES encryption key, no PCI surface.
  * `handle_webhook` returns **PENDING unconditionally** and never reads
    `data.status` for the verdict — Flutterwave's `verif-hash` is a static shared
    secret, weaker than Paystack's HMAC-SHA512, so the webhook is only a trigger
    for a server-to-server `verify()` (wired in webhooks/processor.py).

See docs/payment-multi-provider-design.md sections 5 and 3 (step 3).
"""
from __future__ import annotations

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

logger = logging.getLogger("payments.providers.flutterwave")

_BASE_URL = "https://api.flutterwave.com"
_MIN_GHS = Decimal("1.00")
_UNDERPAY_TOLERANCE = Decimal("0.01")


class FlutterwaveProvider(PaymentProvider):
    _NETWORKS = {
        "mtn_momo": "MTN",
        "vodafone_cash": "VODAFONE",
        "airteltigo": "AIRTELTIGO",
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
        self.secret_key = secret_key or ""
        self.public_key = public_key or ""
        # The "secret hash" the operator sets in their Flutterwave dashboard and
        # which arrives back in the `verif-hash` header. May be unset.
        self.webhook_secret = webhook_secret
        self.callback_url = callback_url or ""

    # -- infrastructure ----------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=15.0, base_url=_BASE_URL)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret_key}"}

    @staticmethod
    def _error_message(exc: httpx.HTTPStatusError) -> str:
        try:
            payload = exc.response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and payload.get("message"):
            return str(payload["message"])
        return exc.response.text or "Flutterwave request failed"

    @staticmethod
    def _local_phone(phone: str) -> str:
        digits = re.sub(r"\D", "", phone or "")
        # PaymentService normalises to 233XXXXXXXXX; Flutterwave GH wants 0XXXXXXXXX.
        if digits.startswith("233") and len(digits) == 12:
            return "0" + digits[3:]
        return digits

    @classmethod
    def _synth_email(cls, phone: Optional[str], internal_reference: str) -> str:
        digits = re.sub(r"\D", "", phone or "")
        if digits:
            return f"{digits}@hotspot.yourisp.com"
        return f"pay_{internal_reference[:12]}@hotspot.yourisp.com"

    # -- initiate --------------------------------------------------------

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
        if amount_ghs < _MIN_GHS:
            raise ValueError("Amount too small for Flutterwave processing")
        if not self.secret_key:
            raise ValueError("Flutterwave secret key is not configured")

        amount = f"{amount_ghs.quantize(Decimal('0.01'))}"
        email = self._synth_email(phone, internal_reference)

        if payment_method == "card":
            return await self._standard_checkout(amount, email, plan_id, site_id, internal_reference)

        network = self._NETWORKS.get(payment_method)
        if not network:
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

        body: dict[str, Any] = {
            "tx_ref": internal_reference,
            "amount": amount,
            "currency": "GHS",
            "network": network,
            "email": email,
            "phone_number": self._local_phone(phone),
            "fullname": "Hotspot Customer",
        }
        if client_ip:
            body["client_ip"] = client_ip
        if self.callback_url:
            body["redirect_url"] = self.callback_url

        client = await self._get_client()
        try:
            response = await client.post(
                "/v3/charges?type=mobile_money_ghana", headers=self._headers(), json=body
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ValueError(self._error_message(exc)) from exc
        payload = response.json()
        logger.info("Flutterwave MoMo charge response: %s", redact_dict(payload if isinstance(payload, dict) else {}))
        return self._initiation_from_charge(payload, internal_reference)

    async def _standard_checkout(
        self, amount: str, email: str, plan_id: str, site_id: str, internal_reference: str
    ) -> PaymentInitiationResult:
        body = {
            "tx_ref": internal_reference,
            "amount": amount,
            "currency": "GHS",
            "redirect_url": self.callback_url or "",
            "customer": {"email": email},
            "payment_options": "card",
            "meta": {"plan_id": plan_id, "site_id": site_id},
        }
        client = await self._get_client()
        try:
            response = await client.post("/v3/payments", headers=self._headers(), json=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ValueError(self._error_message(exc)) from exc
        payload = response.json()
        logger.info("Flutterwave payment init response: %s", redact_dict(payload if isinstance(payload, dict) else {}))
        data = payload.get("data") if isinstance(payload, dict) else None
        link = str((data or {}).get("link") or "")
        if not link:
            raise ValueError(str((payload or {}).get("message") or "Flutterwave did not return a checkout link"))
        return PaymentInitiationResult(
            provider_reference=internal_reference,
            status=PaymentStatus.PENDING,
            next_action=PaymentNextAction.OPEN_URL,
            provider_state="checkout",
            display_message="Continue to Flutterwave to complete your card payment.",
            payment_channel="card",
            authorization_url=link,
            # The captive portal reads the redirect target from provider_payload.
            provider_payload={**(data or {}), "authorization_url": link},
        )

    def _initiation_from_charge(self, payload: dict[str, Any], internal_reference: str) -> PaymentInitiationResult:
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data or {}
        meta = payload.get("meta") if isinstance(payload, dict) else None
        auth = (meta or {}).get("authorization") if isinstance(meta, dict) else None
        auth = auth or {}
        status_raw = str(data.get("status") or "").lower()

        if status_raw == "failed":
            return PaymentInitiationResult(
                provider_reference=internal_reference,
                status=PaymentStatus.FAILED,
                failure_reason=str(data.get("processor_response") or payload.get("message") or "charge failed"),
                next_action=PaymentNextAction.NONE,
                provider_state="failed",
                display_message="Payment failed.",
                payment_channel="mobile_money",
                provider_payload=data,
            )

        mode = str(auth.get("mode") or "").lower()
        if mode == "redirect":
            redirect_url = str(auth.get("redirect") or "")
            return PaymentInitiationResult(
                provider_reference=internal_reference,
                status=PaymentStatus.PENDING,
                next_action=PaymentNextAction.OPEN_URL,
                provider_state="redirect",
                display_message="Continue to authorize the payment.",
                payment_channel="mobile_money",
                authorization_url=redirect_url,
                provider_payload={**data, "authorization_url": redirect_url},
            )
        if mode == "otp":
            return PaymentInitiationResult(
                provider_reference=internal_reference,
                status=PaymentStatus.PENDING,
                next_action=PaymentNextAction.ENTER_OTP,
                provider_state="otp_required",
                display_message="Enter the OTP sent to your phone.",
                payment_channel="mobile_money",
                provider_payload=data,
            )

        return PaymentInitiationResult(
            provider_reference=internal_reference,
            status=PaymentStatus.PENDING,
            next_action=PaymentNextAction.WAIT,
            provider_state=status_raw or "pending",
            display_message="Approve the payment prompt on your phone.",
            payment_channel="mobile_money",
            provider_payload=data,
        )

    # -- verify ---------------------------------------------------------

    async def verify(
        self,
        provider_reference: str,
        expected_amount_ghs: Optional[Decimal] = None,
    ) -> PaymentVerificationResult:
        if not self.secret_key:
            return PaymentVerificationResult(
                status=PaymentStatus.PENDING,
                amount_ghs=Decimal("0.00"),
                provider_reference=provider_reference,
                next_action=PaymentNextAction.WAIT,
                provider_state="pending",
                display_message="Awaiting payment confirmation.",
            )

        client = await self._get_client()
        try:
            response = await client.get(
                "/v3/transactions/verify_by_reference",
                params={"tx_ref": provider_reference},
                headers=self._headers(),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = self._error_message(exc)
            # Flutterwave 400s a not-yet-seen reference — that is "still pending",
            # not a hard failure.
            if exc.response.status_code == 400 and "no transaction" in message.lower():
                return PaymentVerificationResult(
                    status=PaymentStatus.PENDING,
                    amount_ghs=Decimal("0.00"),
                    provider_reference=provider_reference,
                    next_action=PaymentNextAction.WAIT,
                    provider_state="pending",
                    display_message="Awaiting payment confirmation.",
                )
            raise ValueError(message) from exc

        payload = response.json()
        logger.info("Flutterwave verify response: %s", redact_dict(payload if isinstance(payload, dict) else {}))
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data or {}
        status_raw = str(data.get("status") or "").lower()
        currency = str(data.get("currency") or "").upper()
        amount_ghs = Decimal(str(data.get("amount") or "0")).quantize(Decimal("0.01"))
        channel = str(data.get("payment_type") or "mobile_money")

        def _result(status: PaymentStatus, *, failure_reason: str | None = None, state: str, message: str) -> PaymentVerificationResult:
            return PaymentVerificationResult(
                status=status,
                amount_ghs=amount_ghs,
                provider_reference=provider_reference,
                failure_reason=failure_reason,
                next_action=PaymentNextAction.NONE if status != PaymentStatus.PENDING else PaymentNextAction.WAIT,
                provider_state=state,
                display_message=message,
                provider_payload=data,
                payment_channel=channel,
            )

        if status_raw == "successful":
            if currency != "GHS":
                return _result(
                    PaymentStatus.FAILED,
                    failure_reason=f"unexpected currency {currency or 'unknown'}",
                    state="wrong_currency",
                    message="Payment could not be verified.",
                )
            if expected_amount_ghs is not None and amount_ghs < (Decimal(str(expected_amount_ghs)) - _UNDERPAY_TOLERANCE):
                return _result(
                    PaymentStatus.FAILED,
                    failure_reason=f"underpaid: charged GHS {amount_ghs}, expected GHS {expected_amount_ghs}",
                    state="underpaid",
                    message="Payment amount did not match.",
                )
            return _result(PaymentStatus.SUCCESS, state="successful", message="Payment successful.")

        if status_raw == "failed":
            return _result(
                PaymentStatus.FAILED,
                failure_reason=str(data.get("processor_response") or "payment failed"),
                state="failed",
                message="Payment failed.",
            )

        return _result(PaymentStatus.PENDING, state=status_raw or "pending", message="Awaiting payment confirmation.")

    async def verify_credentials(self) -> None:
        if not self.secret_key:
            raise ValueError("Flutterwave secret key is not configured")
        client = self._client or httpx.AsyncClient(timeout=15.0, base_url=_BASE_URL)
        try:
            response = await client.get("/v3/transactions", params={"page": 1}, headers=self._headers())
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ValueError(self._error_message(exc)) from exc
        finally:
            if self._client is None:
                await client.aclose()

    # -- webhook ------------------------------------------------------

    async def handle_webhook(self, headers, raw_body: bytes) -> PaymentWebhookResult:
        got = headers.get("verif-hash") or headers.get("Verif-Hash")
        if not self.webhook_secret or not got or not hmac.compare_digest(got, self.webhook_secret):
            raise ValueError("Invalid flutterwave webhook signature")

        payload = json.loads(raw_body.decode("utf-8"))
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        tx_ref = str(data.get("tx_ref") or "")
        if not tx_ref:
            raise ValueError("Webhook payload missing tx_ref")

        # PENDING regardless of data.status — the processor re-verifies
        # server-to-server. verif-hash is a static shared secret, not an HMAC.
        return PaymentWebhookResult(
            internal_reference=tx_ref,
            status=PaymentStatus.PENDING,
            provider_reference=tx_ref,
            provider_state=str(data.get("status") or "webhook"),
            display_message="Verifying payment...",
            provider_payload=payload if isinstance(payload, dict) else None,
            payment_channel=str(data.get("payment_type") or "mobile_money"),
        )

    # -- follow-ups -------------------------------------------------

    async def submit_otp(self, reference: str, otp: str) -> PaymentInitiationResult:
        if not self.secret_key:
            return PaymentInitiationResult(
                provider_reference=reference,
                status=PaymentStatus.PENDING,
                next_action=PaymentNextAction.WAIT,
                provider_state="pending",
                display_message="Awaiting payment confirmation.",
                payment_channel="mobile_money",
            )
        client = await self._get_client()
        try:
            # validate-charge needs Flutterwave's flw_ref; resolve it from our tx_ref.
            lookup = await client.get(
                "/v3/transactions/verify_by_reference",
                params={"tx_ref": reference},
                headers=self._headers(),
            )
            lookup.raise_for_status()
            flw_ref = str((lookup.json().get("data") or {}).get("flw_ref") or "")
            if not flw_ref:
                raise ValueError("Could not resolve Flutterwave reference for OTP validation")
            response = await client.post(
                "/v3/validate-charge",
                headers=self._headers(),
                json={"type": "mobile_money_ghana", "flw_ref": flw_ref, "otp": otp},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ValueError(self._error_message(exc)) from exc
        payload = response.json()
        logger.info("Flutterwave validate-charge response: %s", redact_dict(payload if isinstance(payload, dict) else {}))
        return self._initiation_from_charge(payload, reference)
