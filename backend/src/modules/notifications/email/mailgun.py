from __future__ import annotations
import logging
import httpx
from src.modules.notifications.email.base import EmailProvider, EmailSendResult

logger = logging.getLogger("email.mailgun")


class MailgunProvider(EmailProvider):
    def __init__(self, api_key: str, domain: str, from_email: str) -> None:
        self._api_key = api_key
        self._domain = domain
        self._from_email = from_email

    async def send(self, to: str, subject: str, body_html: str, body_text: str) -> EmailSendResult:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"https://api.mailgun.net/v3/{self._domain}/messages",
                    auth=("api", self._api_key),
                    data={
                        "from": self._from_email,
                        "to": to,
                        "subject": subject,
                        "text": body_text,
                        "html": body_html,
                    },
                )
            if resp.status_code == 200:
                data = resp.json()
                return EmailSendResult(success=True, provider_reference=data.get("id"))
            logger.warning("mailgun_send_failed status=%s body=%s", resp.status_code, resp.text[:200])
            return EmailSendResult(success=False, error=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            logger.error("mailgun_send_error error=%s", exc)
            return EmailSendResult(success=False, error=str(exc))
