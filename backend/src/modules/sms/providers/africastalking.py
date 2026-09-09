from __future__ import annotations

import logging
from typing import Optional

import httpx

from src.modules.sms.providers.base import SMSProvider
from src.modules.sms.types import SMSSendResult

logger = logging.getLogger("sms.providers.africastalking")

# Per-recipient statusCode: 100 Processed / 101 Sent / 102 Queued are accepted;
# anything else (403 InvalidPhoneNumber, 405 InsufficientBalance,
# 406 UserInBlacklist, 407 CouldNotRoute, 5xx gateway) is a failed send.
_AT_ACCEPTED_CODES = {100, 101, 102}


def _clip(text: str, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AfricasTalkingSMSProvider(SMSProvider):
    """
    Africa's Talking SMS API.

    POST https://api.africastalking.com/version1/messaging
    Header: apiKey
    Body (form): username, to, message, from
    Success body: {"SMSMessageData": {"Recipients": [{"statusCode": 101, "messageId": "..."}]}}
    """

    def __init__(
        self,
        *,
        api_key: str,
        username: str,
        sender_id: str,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.api_key = api_key
        self.username = username
        self.sender_id = sender_id
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=10.0, base_url="https://api.africastalking.com")

    async def send(self, to: str, message: str) -> SMSSendResult:
        if not (self.api_key and self.username and self.sender_id):
            return SMSSendResult(success=False, error="africastalking_not_configured")

        headers = {"apiKey": self.api_key, "Accept": "application/json"}
        data = {
            "username": self.username,
            "to": to,
            "message": message,
            "from": self.sender_id,
        }
        client = await self._get_client()
        try:
            resp = await client.post("/version1/messaging", headers=headers, data=data)
            try:
                payload = resp.json()
            except Exception:
                payload = None

            if resp.status_code >= 400:
                reason = None
                if isinstance(payload, dict):
                    smd = payload.get("SMSMessageData")
                    reason = (smd or {}).get("Message") if isinstance(smd, dict) else None
                    reason = reason or payload.get("message") or payload.get("error")
                reason = reason or resp.text
                logger.warning("africastalking_sms_send_failed status=%s body=%s", resp.status_code, _clip(reason, 500))
                err = f"africastalking_http_{resp.status_code}"
                if reason:
                    err += f": {_clip(reason)}"
                return SMSSendResult(success=False, error=err)

            recipient = None
            if isinstance(payload, dict):
                smd = payload.get("SMSMessageData")
                recips = smd.get("Recipients") if isinstance(smd, dict) else None
                if isinstance(recips, list) and recips:
                    recipient = recips[0]

            if isinstance(recipient, dict):
                try:
                    code = int(recipient.get("statusCode"))
                except (TypeError, ValueError):
                    code = None
                if code is not None and code not in _AT_ACCEPTED_CODES:
                    status_text = recipient.get("status") or f"statusCode {code}"
                    logger.warning("africastalking_sms_rejected code=%s status=%s", code, status_text)
                    return SMSSendResult(success=False, error=f"africastalking_{_clip(str(status_text), 80)}")
                provider_ref = recipient.get("messageId") or recipient.get("message_id")
                logger.info("africastalking_sms_send_ok to=%s provider_reference=%s", to, provider_ref)
                return SMSSendResult(success=True, provider_reference=str(provider_ref) if provider_ref else None)

            # 2xx but no recipient block — accept it, we have nothing to object to.
            logger.info("africastalking_sms_send_ok to=%s provider_reference=None", to)
            return SMSSendResult(success=True, provider_reference=None)
        except Exception as e:
            logger.exception("africastalking_sms_send_exception to=%s", to)
            return SMSSendResult(success=False, error=f"africastalking_exception: {_clip(str(e))}")
