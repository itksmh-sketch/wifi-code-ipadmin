from __future__ import annotations

import base64
import logging
from typing import Optional

import httpx

from src.config import Settings
from src.modules.sms.providers.base import SMSProvider
from src.modules.sms.types import SMSSendResult

logger = logging.getLogger("sms.providers.hubtel")


class HubtelSMSProvider(SMSProvider):
    """
    Hubtel SMS API.

    POST https://smsc.hubtel.com/v1/messages/send
    Basic Auth: client_id:client_secret
    JSON body: { From, To, Content }
    """

    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None) -> None:
        self.settings = settings
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=10.0, base_url="https://smsc.hubtel.com")

    def _auth_header(self) -> str:
        raw = f"{self.settings.hubtel_client_id}:{self.settings.hubtel_client_secret}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    async def send(self, to: str, message: str) -> SMSSendResult:
        if not self.settings.hubtel_client_id or not self.settings.hubtel_client_secret or not self.settings.hubtel_from:
            return SMSSendResult(success=False, error="hubtel_not_configured")

        body = {"From": self.settings.hubtel_from, "To": to, "Content": message}
        headers = {"Authorization": self._auth_header()}

        client = await self._get_client()
        try:
            resp = await client.post("/v1/messages/send", headers=headers, json=body)
            payload = None
            try:
                payload = resp.json()
            except Exception:
                payload = None

            if resp.status_code >= 400:
                logger.warning("hubtel_sms_send_failed status=%s body=%s", resp.status_code, payload or resp.text)
                return SMSSendResult(success=False, error=f"hubtel_http_{resp.status_code}")

            provider_ref = None
            if isinstance(payload, dict):
                provider_ref = payload.get("MessageId") or payload.get("messageId") or payload.get("id")
            logger.info("hubtel_sms_send_ok to=%s provider_reference=%s", to, provider_ref)
            return SMSSendResult(success=True, provider_reference=str(provider_ref) if provider_ref else None)
        except Exception as e:
            logger.exception("hubtel_sms_send_exception to=%s", to)
            return SMSSendResult(success=False, error=str(e))

