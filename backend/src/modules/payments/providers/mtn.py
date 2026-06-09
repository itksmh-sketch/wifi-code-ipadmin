import base64
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import httpx

from src.config import Settings
from src.modules.payments.providers.base import PaymentProvider
from src.modules.payments.providers.utils import redact_dict
from src.modules.payments.types import (
    PaymentInitiationResult,
    PaymentStatus,
    PaymentVerificationResult,
    PaymentWebhookResult,
)

logger = logging.getLogger("payments.providers.mtn")


class MTNMoMoProvider(PaymentProvider):
    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None) -> None:
        self.settings = settings
        self._client = client
        self._token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=10.0, base_url=self.settings.mtn_momo_base_url.rstrip("/"))

    def _mock_mode(self) -> bool:
        return not bool(self.settings.mtn_momo_base_url and self.settings.mtn_momo_collection_subscription_key)

    def _token_is_fresh(self) -> bool:
        if not self._token or not self._token_expires_at:
            return False
        return datetime.now(timezone.utc) + timedelta(seconds=60) < self._token_expires_at

    async def _get_token(self) -> str:
        if self._token_is_fresh():
            return self._token  # type: ignore[return-value]

        basic = base64.b64encode(
            f"{self.settings.mtn_momo_api_user}:{self.settings.mtn_momo_api_key}".encode("utf-8")
        ).decode("utf-8")
        headers = {
            "Authorization": f"Basic {basic}",
            "Ocp-Apim-Subscription-Key": self.settings.mtn_momo_collection_subscription_key,
        }
        client = await self._get_client()
        response = await client.post("/collection/token/", headers=headers)
        response.raise_for_status()
        payload = response.json()
        logger.info("MTN token response: %s", redact_dict(payload if isinstance(payload, dict) else {}))
        access_token = payload.get("access_token")
        if not access_token:
            raise ValueError("MTN token response missing access_token")
        expires_in = int(payload.get("expires_in", 3600))
        self._token = access_token
        self._token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        return access_token

    async def initiate(
        self,
        amount_ghs: Decimal,
        phone: Optional[str],
        plan_id: str,
        site_id: str,
        internal_reference: str,
        payment_method: str,
    ) -> PaymentInitiationResult:
        if not phone:
            return PaymentInitiationResult(
                provider_reference=None,
                status=PaymentStatus.FAILED,
                failure_reason="Phone number is required for MTN MoMo",
            )

        if self._mock_mode():
            logger.warning("MTN provider running in mock mode due to missing credentials/base URL")
            return PaymentInitiationResult(provider_reference=internal_reference, status=PaymentStatus.PENDING)

        token = await self._get_token()
        reference_id = internal_reference
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Reference-Id": reference_id,
            "X-Target-Environment": self.settings.mtn_momo_environment,
            "Ocp-Apim-Subscription-Key": self.settings.mtn_momo_collection_subscription_key,
            "Content-Type": "application/json",
        }
        body = {
            "amount": f"{amount_ghs:.2f}",
            "currency": "GHS",
            "externalId": internal_reference,
            "payer": {"partyIdType": "MSISDN", "partyId": phone},
            "payerMessage": f"WiFi plan {plan_id}",
            "payeeNote": f"Site {site_id}",
        }
        client = await self._get_client()
        resp = await client.post("/collection/v1_0/requesttopay", headers=headers, json=body)
        logger.info("MTN initiate status=%s body=%s", resp.status_code, redact_dict(body))
        if resp.status_code >= 400:
            return PaymentInitiationResult(
                provider_reference=reference_id,
                status=PaymentStatus.FAILED,
                failure_reason=f"MTN request failed with status {resp.status_code}",
            )
        return PaymentInitiationResult(provider_reference=reference_id, status=PaymentStatus.PENDING)

    async def verify(self, provider_reference: str) -> PaymentVerificationResult:
        if self._mock_mode():
            return PaymentVerificationResult(
                status=PaymentStatus.SUCCESS,
                amount_ghs=Decimal("0.00"),
                provider_reference=provider_reference,
            )
        token = await self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Target-Environment": self.settings.mtn_momo_environment,
            "Ocp-Apim-Subscription-Key": self.settings.mtn_momo_collection_subscription_key,
        }
        client = await self._get_client()
        resp = await client.get(f"/collection/v1_0/requesttopay/{provider_reference}", headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        logger.info("MTN verify payload=%s", redact_dict(payload if isinstance(payload, dict) else {}))
        provider_status = str(payload.get("status", "PENDING")).upper()
        amount = Decimal(str(payload.get("amount", "0")))
        reason = payload.get("reason") or payload.get("financialTransactionId")
        if provider_status == "SUCCESSFUL":
            mapped = PaymentStatus.SUCCESS
        elif provider_status in {"FAILED", "REJECTED"}:
            mapped = PaymentStatus.FAILED
        else:
            mapped = PaymentStatus.PENDING
        return PaymentVerificationResult(
            status=mapped,
            amount_ghs=amount,
            provider_reference=provider_reference,
            failure_reason=None if mapped != PaymentStatus.FAILED else str(reason or "payment failed"),
        )

    async def handle_webhook(self, headers, raw_body: bytes) -> PaymentWebhookResult:
        # MTN verification is completed via callback verify() by reference.
        payload = json.loads(raw_body.decode("utf-8"))
        reference = payload.get("externalId") or payload.get("referenceId")
        if not reference:
            raise ValueError("MTN webhook missing reference")
        status = str(payload.get("status", "PENDING")).upper()
        mapped = PaymentStatus.PENDING
        if status == "SUCCESSFUL":
            mapped = PaymentStatus.SUCCESS
        elif status in {"FAILED", "REJECTED"}:
            mapped = PaymentStatus.FAILED
        return PaymentWebhookResult(
            internal_reference=str(reference),
            status=mapped,
            provider_reference=str(payload.get("financialTransactionId") or reference),
        )
