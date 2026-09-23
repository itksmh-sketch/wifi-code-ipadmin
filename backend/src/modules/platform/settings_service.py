"""Platform settings: DB-backed key/value store, seeded from .env (migration 014).

Reads fall back to the .env/config default when a key is absent from the table,
so the system keeps working even before the table is seeded. See
[[platform-settings-requirement]].
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.models import PlatformSetting

settings = get_settings()
logger = logging.getLogger("platform.settings")

# Keys exposed via the platform settings table -> config attribute for defaults.
PLATFORM_SETTING_KEYS = {
    "wg_server_endpoint": "wg_server_endpoint",
    "platform_app_url": "platform_app_url",
    "webhook_base_url": "webhook_base_url",
    # Shown to applicants in the application-received/rejected messages.
    # Seeded by migration 049 with the real address.
    "platform_support_email": "platform_support_email",
    # Free-trial length stamped onto an operator at approval. Unset falls back
    # to TRIAL_DAYS; read through applications.service.get_trial_days.
    "platform_trial_days": "trial_days",
    # The name operators see in notification emails/SMS. Unset falls back to
    # the email sender name, which is what the From: line already says.
    "platform_name": "sendgrid_from_name",
}

PLATFORM_NAME_MAX_LENGTH = 30
_PLATFORM_NAME_FALLBACK = "IpAdmin"


def _default(key: str) -> str:
    attr = PLATFORM_SETTING_KEYS.get(key)
    return str(getattr(settings, attr, "") or "") if attr else ""


async def get_setting(db: AsyncSession, key: str) -> str:
    """Return a setting value from the table, or the .env/config default."""
    row = await db.execute(select(PlatformSetting.value).where(PlatformSetting.key == key))
    value = row.scalar_one_or_none()
    if value is None or value == "":
        return _default(key)
    return value


async def get_platform_name() -> str:
    """The platform's name for operator-facing messages. Never raises.

    Own short-lived session: callers are notification senders with no session
    to borrow, and some are security alerts that must go out even when this
    lookup fails — so any failure degrades to config, then to "IpAdmin".
    """
    try:
        from src.db.base import async_session_factory

        async with async_session_factory() as db:
            name = await get_setting(db, "platform_name")
    except Exception as exc:
        logger.error("platform_name_lookup_failed error=%s — using config", exc)
        name = _default("platform_name")
    return name.strip() or _PLATFORM_NAME_FALLBACK


async def get_all_settings(db: AsyncSession) -> dict[str, str]:
    """Return all known platform settings (table value, or default if missing)."""
    rows = await db.execute(select(PlatformSetting.key, PlatformSetting.value))
    stored = {k: v for k, v in rows.all()}
    return {key: (stored.get(key) or _default(key)) for key in PLATFORM_SETTING_KEYS}


async def set_setting(db: AsyncSession, key: str, value: str) -> None:
    """Upsert a setting value. Caller commits."""
    existing = await db.execute(select(PlatformSetting).where(PlatformSetting.key == key))
    row = existing.scalar_one_or_none()
    if row is None:
        db.add(PlatformSetting(key=key, value=value))
    else:
        row.value = value
    await db.flush()
