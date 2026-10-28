"""The values the public pages (/ and /guides) show: the monthly fee, the trial
length and the support email a new operator gets today, plus the public app
URL for the link-preview tags.

Read the way operator approval reads them (platform setting, then .env, then
the code fallback), and kept in memory for a minute, so a page view normally
touches no database and a Settings edit shows within TTL_SECONDS. If a refresh
fails, the last good values are served and the refresh is retried after
RETRY_SECONDS. Only when nothing has ever loaded does get_public_values()
return None, and the route answers 503.

One copy per process: uvicorn runs a single process today, and more workers
would each keep their own, which is harmless.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from src.config import get_settings
from src.db.base import async_session_factory
from src.modules.applications.service import _TRIAL_DAYS_FALLBACK, TRIAL_SETTING_KEY, _coerce_trial_days
from src.modules.billing.service import get_default_monthly_fee
from src.modules.platform.settings_service import get_setting

logger = logging.getLogger("admin_portal.public_values")

TTL_SECONDS = 60
RETRY_SECONDS = 10


@dataclass(frozen=True)
class PublicValues:
    fee: str
    trial_days: int
    support_email: str
    app_url: str


def format_fee(fee: Decimal) -> str:
    """80.00 -> "80", 80.50 -> "80.50", 1200.00 -> "1,200"."""
    return f"{fee:,.0f}" if fee == fee.to_integral_value() else f"{fee:,.2f}"


async def _load() -> PublicValues:
    async with async_session_factory() as db:
        fee = await get_default_monthly_fee(db)
        # Not get_trial_days(): it catches a database error and returns the
        # .env value, which would then be cached here as if it were good.
        trial = _coerce_trial_days(await get_setting(db, TRIAL_SETTING_KEY))
        email = await get_setting(db, "platform_support_email")
        app_url = await get_setting(db, "platform_app_url")
    if trial is None:
        trial = _coerce_trial_days(get_settings().trial_days) or _TRIAL_DAYS_FALLBACK
    return PublicValues(format_fee(fee), trial, email.strip(), app_url.strip().rstrip("/"))


_clock = time.monotonic
_lock = asyncio.Lock()
_values: PublicValues | None = None
_fresh_until = 0.0
_failing = False


async def get_public_values() -> PublicValues | None:
    global _values, _fresh_until, _failing
    if _values is not None and _clock() < _fresh_until:
        return _values
    async with _lock:
        # Another request may have refreshed (or just failed) while this one waited.
        if _clock() < _fresh_until:
            return _values
        try:
            values = await _load()
        except Exception:
            if not _failing:
                logger.exception("public_values_refresh_failed serving=%s", "last good" if _values else "nothing (503)")
            _failing = True
            _fresh_until = _clock() + RETRY_SECONDS
            return _values
        if _failing:
            logger.warning("public_values_refresh_recovered")
        _values, _fresh_until, _failing = values, _clock() + TTL_SECONDS, False
        return _values


def reset() -> None:
    """Forget everything (tests). A new lock too: each test has its own event loop."""
    global _lock, _values, _fresh_until, _failing
    _lock = asyncio.Lock()
    _values, _fresh_until, _failing = None, 0.0, False
