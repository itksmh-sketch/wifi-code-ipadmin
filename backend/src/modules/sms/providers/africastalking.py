from __future__ import annotations

import logging
from typing import Optional

import httpx

from src.config import Settings
from src.modules.sms.providers.base import SMSProvider
from src.modules.sms.types import SMSSendResult

logger = logging.getLogger("sms.providers.africastalking")


class AfricasTalkingSMSProvider(SMSProvider):
    """
    Africa's Talking SMS API.

    POST https://api.africastalking.com/version1/messaging
    Header: apiKey
    Body (form): username, to, message, from
    """

    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None) -> None:
        self.settings = settings
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=10.0, base_url="https://api.africastalking.com")

    async def send(self, to: str, message: str) -> SMSSendResult:
        if (
            not self.settings.africastalking_api_key
            or not self.settings.africastalking_username
            or not self.settings.africastalking_from
        ):
            return SMSSendResult(success=False, error="africastalking_not_configured")

        headers = {"apiKey": self.settings.africastalking_api_key}
        data = {
            "username": self.settings.africastalking_username,
            "to": to,
            "message": message,
            "from": self.settings.africastalking_from,
        }
        client = await self._get_client()
        try:
            resp = await client.post("/version1/messaging", headers=headers, data=data)
            payload = None
            try:
                payload = resp.json()
            except Exception:
                payload = None

            if resp.status_code >= 400:
                logger.warning("africastalking_sms_send_failed status=%s body=%s", resp.status_code, payload or resp.text)
                return SMSSendResult(success=False, error=f"africastalking_http_{resp.status_code}")

            provider_ref = None
            if isinstance(payload, dict):
                # Common shape: {"SMSMessageData":{"Recipients":[{"messageId": "..."}]}}
                recips = (payload.get("SMSMessageData") or {}).get("Recipients") if isinstance(payload.get("SMSMessageData"), dict) else None
                if isinstance(recips, list) and recips:
                    provider_ref = recips[0].get("messageId") or recips[0].get("message_id")
            logger.info("africastalking_sms_send_ok to=%s provider_reference=%s", to, provider_ref)
            return SMSSendResult(success=True, provider_reference=str(provider_ref) if provider_ref else None)
        except Exception as e:
            logger.exception("africastalking_sms_send_exception to=%s", to)
            return SMSSendResult(success=False, error=str(e))

