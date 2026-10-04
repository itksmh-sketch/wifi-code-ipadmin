"""Shared construction of the per-operator payment-provider webhook URL.

One helper so the URL shape stays in one place, matching the explicit routes
registered in ``src.modules.webhooks.routes`` (one route per provider, e.g.
``/api/v1/webhooks/paystack/{operator_slug}``).
"""
from __future__ import annotations

import logging

from src.config import get_settings
from src.db.base import async_session_factory

logger = logging.getLogger("webhooks.urls")


async def webhook_base_url() -> str:
    """The public base the webhook URL is built on.

    Read from the platform_settings table, not straight from config: that row
    is the source of truth a platform owner edits in the portal's Settings
    page, and on this deployment it holds the public HTTPS domain while the
    .env value is the bare server address (and Paystack only accepts HTTPS
    webhook URLs). Its own short-lived session, so a failed lookup can't leave
    the caller's transaction aborted; on failure it falls back to config,
    which is also get_setting's own default when the row is empty.
    """
    from src.modules.platform.settings_service import get_setting

    try:
        async with async_session_factory() as db:
            base = await get_setting(db, "webhook_base_url")
    except Exception as exc:
        logger.error("webhook_base_url_lookup_failed error=%s", exc)
        base = get_settings().webhook_base_url
    return (base or "").strip().rstrip("/")


def build_webhook_url(base: str, provider_key: str, slug: str | None) -> str | None:
    """``base`` comes from webhook_base_url()."""
    base = (base or "").strip().rstrip("/")
    if not base or not slug:
        return None
    return f"{base}/api/v1/webhooks/{provider_key}/{slug}"
