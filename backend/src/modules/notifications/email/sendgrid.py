from __future__ import annotations
import logging
import httpx
from src.modules.notifications.email.base import EmailProvider, EmailSendResult

logger = logging.getLogger("email.sendgrid")


class SendGridProvider(EmailProvider):
    def __init__(self, api_key: str, from_email: str, from_name: str) -> None:
        self._api_key = api_key
        self._from_email = from_email
        self._from_name = from_name

    async def send(self, to: str, subject: str, body_html: str, body_text: str) -> EmailSendResult:
        payload = {
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": self._from_email, "name": self._from_name},
            "subject": subject,
            "content": [
                {"type": "text/plain", "value": body_text},
                {"type": "text/html", "value": body_html},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            if resp.status_code in (200, 202):
                return EmailSendResult(success=True, provider_reference=resp.headers.get("X-Message-Id"))
            logger.warning("sendgrid_send_failed status=%s body=%s", resp.status_code, resp.text[:200])
            return EmailSendResult(success=False, error=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            logger.error("sendgrid_send_error error=%s", exc)
            return EmailSendResult(success=False, error=str(exc))
