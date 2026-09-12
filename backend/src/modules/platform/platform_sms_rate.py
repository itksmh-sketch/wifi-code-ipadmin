"""Current per-segment rate for the platform-provided SMS gateway.

Fails closed: a send that can't be priced must not happen. Callers resolve
the rate BEFORE calling the provider's send() — not after — so an unset rate
blocks the send entirely rather than producing an unpriced usage row (see
sms.metering).
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ProviderCatalogEntry

ARKESEL_PLATFORM_CATALOG_KEY = "arkesel_platform"

__all__ = ["PlatformSMSRateNotConfigured", "get_current_platform_sms_rate"]


class PlatformSMSRateNotConfigured(Exception):
    """No catalog row, or no rate set on it, for the platform SMS gateway."""


async def get_current_platform_sms_rate(db: AsyncSession) -> Decimal:
    entry = (
        await db.execute(
            select(ProviderCatalogEntry).where(
                ProviderCatalogEntry.category == "sms",
                ProviderCatalogEntry.provider_key == ARKESEL_PLATFORM_CATALOG_KEY,
            )
        )
    ).scalar_one_or_none()
    if entry is None or entry.platform_rate_per_segment is None:
        raise PlatformSMSRateNotConfigured(
            "platform_rate_per_segment is not set on the arkesel_platform catalog row"
        )
    return Decimal(entry.platform_rate_per_segment)
