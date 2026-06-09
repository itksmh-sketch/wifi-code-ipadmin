from __future__ import annotations

import logging

from src.config import Settings
from src.db.models import Plan
from src.modules.sms.providers.africastalking import AfricasTalkingSMSProvider
from src.modules.sms.providers.base import SMSProvider
from src.modules.sms.providers.hubtel import HubtelSMSProvider
from src.modules.sms.types import SMSSendResult

logger = logging.getLogger("sms.service")


def _format_duration(plan: Plan) -> str:
    # time / data / hybrid
    parts: list[str] = []
    if plan.type in ("time", "hybrid") and plan.duration_minutes:
        mins = int(plan.duration_minutes)
        if mins >= 60 and mins % 60 == 0:
            parts.append(f"{mins // 60} hrs")
        else:
            parts.append(f"{mins} mins")
    if plan.type in ("data", "hybrid") and plan.data_limit_mb:
        mb = int(plan.data_limit_mb)
        if mb >= 1024 and mb % 1024 == 0:
            parts.append(f"{mb // 1024} GB")
        elif mb >= 1024:
            parts.append(f"{mb / 1024:.1f} GB")
        else:
            parts.append(f"{mb} MB")
    return " + ".join(parts) if parts else "N/A"


def _truncate(s: str, max_len: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max(0, max_len - 1)].rstrip() + "…"


def build_voucher_sms_message(*, code: str, plan: Plan) -> str:
    # Must stay under 160 chars; we enforce by truncating plan name.
    duration = _format_duration(plan)
    plan_name = _truncate(plan.name, 24)
    msg = f"Your WiFi voucher: {code}. Plan: {plan_name}. Valid for {duration}. Connect at the login page. Enjoy!"
    if len(msg) <= 160:
        return msg
    # Tighten plan name further to respect 160-char constraint.
    plan_name = _truncate(plan.name, 12)
    msg = f"Your WiFi voucher: {code}. Plan: {plan_name}. Valid for {duration}. Connect at the login page. Enjoy!"
    return msg[:160]


class SMSService:
    def __init__(self, settings: Settings, provider: SMSProvider | None) -> None:
        self.settings = settings
        self.provider = provider

    @property
    def enabled(self) -> bool:
        return self.provider is not None

    async def send(self, *, to: str, message: str) -> SMSSendResult | None:
        if not self.provider:
            return None
        return await self.provider.send(to=to, message=message)


def build_sms_service(settings: Settings) -> SMSService:
    name = (settings.sms_provider or "").strip().lower()
    provider: SMSProvider | None = None

    if name == "hubtel":
        provider = HubtelSMSProvider(settings)
    elif name in {"africastalking", "africas_talking", "africas-talking"}:
        provider = AfricasTalkingSMSProvider(settings)
    elif name:
        logger.warning("sms_provider_unrecognized value=%s sms_disabled=true", name)
        provider = None
    else:
        logger.warning("sms_provider_missing sms_disabled=true")
        provider = None

    return SMSService(settings=settings, provider=provider)

