from __future__ import annotations

import base64
import logging
from typing import Optional

import httpx

from src.modules.sms.providers.base import SMSProvider
from src.modules.sms.types import SMSSendResult

logger = logging.getLogger("sms.providers.hubtel")

# Hubtel's send response uses `status: 0` for "request submitted successfully";
# any other value means the message was accepted by the endpoint but not queued.
_HUBTEL_OK_STATUS = 0


def _clip(text: str, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class HubtelSMSProvider(SMSProvider):
    """
    Hubtel SMS API.

    POST https://smsc.hubtel.com/v1/messages/send
    Basic Auth: base64(client_id:client_secret)
    JSON body: { From, To, Content }
    Success body: { "status": 0, "messageId": "...", "statusDescription": "..." }
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        sender_id: str,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.sender_id = sender_id
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=10.0, base_url="https://smsc.hubtel.com")

    def _auth_header(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    async def send(self, to: str, message: str) -> SMSSendResult:
        if not (self.client_id and self.client_secret and self.sender_id):
            return SMSSendResult(success=False, error="hubtel_not_configured")

        body = {"From": self.sender_id, "To": to, "Content": message}
        headers = {"Authorization": self._auth_header()}

        client = await self._get_client()
        try:
            resp = await client.post("/v1/messages/send", headers=headers, json=body)
            try:
                payload = resp.json()
            except Exception:
                payload = None

            if resp.status_code >= 400:
                reason = None
                if isinstance(payload, dict):
                    reason = payload.get("message") or payload.get("Message") or payload.get("statusDescription")
                reason = reason or resp.text
                logger.warning("hubtel_sms_send_failed status=%s body=%s", resp.status_code, _clip(reason, 500))
                err = f"hubtel_http_{resp.status_code}"
                if reason:
                    err += f": {_clip(reason)}"
                return SMSSendResult(success=False, error=err)

            # HTTP 2xx: Hubtel still reports queue-acceptance in the body.
            if isinstance(payload, dict) and payload.get("status") not in (None, _HUBTEL_OK_STATUS, str(_HUBTEL_OK_STATUS)):
                desc = payload.get("statusDescription") or payload.get("message") or "message not accepted"
                logger.warning("hubtel_sms_rejected status=%s desc=%s", payload.get("status"), desc)
                return SMSSendResult(success=False, error=f"hubtel_status_{payload.get('status')}: {_clip(str(desc))}")

            provider_ref = None
            if isinstance(payload, dict):
                provider_ref = payload.get("messageId") or payload.get("MessageId") or payload.get("id")
            logger.info("hubtel_sms_send_ok to=%s provider_reference=%s", to, provider_ref)
            return SMSSendResult(success=True, provider_reference=str(provider_ref) if provider_ref else None)
        except Exception as e:
            logger.exception("hubtel_sms_send_exception to=%s", to)
            return SMSSendResult(success=False, error=f"hubtel_exception: {_clip(str(e))}")
