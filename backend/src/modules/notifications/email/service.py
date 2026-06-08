from __future__ import annotations
import logging
from functools import lru_cache
from src.config import get_settings
from src.modules.notifications.email.base import EmailProvider, EmailSendResult
from src.modules.notifications.email.sendgrid import SendGridProvider
from src.modules.notifications.email.mailgun import MailgunProvider

logger = logging.getLogger("email.service")


class EmailService:
    def __init__(self, provider: EmailProvider | None) -> None:
        self._provider = provider

    @property
    def enabled(self) -> bool:
        return self._provider is not None

    async def send(self, to: str, subject: str, body_html: str, body_text: str) -> EmailSendResult | None:
        if not self._provider:
            logger.info("email_disabled to=%s subject=%s", to, subject)
            return None
        result = await self._provider.send(to=to, subject=subject, body_html=body_html, body_text=body_text)
        if not result.success:
            logger.warning("email_send_failed to=%s subject=%s error=%s", to, subject, result.error)
        return result


def build_email_service() -> EmailService:
    settings = get_settings()
    name = (settings.email_provider or "").strip().lower()
    provider: EmailProvider | None = None

    if name == "sendgrid" and settings.sendgrid_api_key:
        provider = SendGridProvider(
            api_key=settings.sendgrid_api_key,
            from_email=settings.sendgrid_from_email,
            from_name=settings.sendgrid_from_name,
        )
    elif name == "mailgun" and settings.mailgun_api_key and settings.mailgun_domain:
        provider = MailgunProvider(
            api_key=settings.mailgun_api_key,
            domain=settings.mailgun_domain,
            from_email=settings.mailgun_from_email or f"noreply@{settings.mailgun_domain}",
        )
    elif name:
        logger.warning("email_provider_unrecognised value=%s email_disabled=true", name)

    if provider is None:
        logger.warning("email_provider_missing email_disabled=true")

    return EmailService(provider=provider)


@lru_cache()
def get_email_service() -> EmailService:
    return build_email_service()
