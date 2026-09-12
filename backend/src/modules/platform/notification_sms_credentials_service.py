"""Platform notification SMS credentials — the account used to text OPERATORS
about their own account (trial expiry, invoices, suspension).

Deliberately separate from platform_sms_credentials_service, which resolves the
gateway operators resell to their customers. See PlatformNotificationSMSCredential's
model docstring for why these must be different Arkesel accounts rather than
just different rows.

No .env fallback and no process-wide cache: notifications are low volume (a
handful a day), so resolving per send costs one indexed SELECT and removes any
possibility of serving keys the admin has since changed through the settings
card. The previous Settings-backed singleton could not see such a change until
a backend restart.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import PlatformNotificationSMSCredential
from src.modules.credentials.service import load_credentials

ARKESEL = "arkesel"

__all__ = ["ARKESEL", "get_active_credential", "get_credential", "resolve_notification_sms"]


async def get_active_credential(db: AsyncSession) -> Optional[PlatformNotificationSMSCredential]:
    """The single active row, or None if notifications are unconfigured."""
    return (
        await db.execute(
            select(PlatformNotificationSMSCredential).where(
                PlatformNotificationSMSCredential.is_active.is_(True)
            )
        )
    ).scalar_one_or_none()


async def get_credential(db: AsyncSession, provider: str = ARKESEL) -> Optional[PlatformNotificationSMSCredential]:
    """The stored row for a provider, active or not."""
    return (
        await db.execute(
            select(PlatformNotificationSMSCredential).where(
                PlatformNotificationSMSCredential.provider == provider
            )
        )
    ).scalar_one_or_none()


async def resolve_notification_sms(db: AsyncSession) -> tuple[str, dict] | None:
    """``(provider_key, credentials)`` for notification sends, or None when
    nothing is configured — in which case the caller skips, exactly as the
    old disabled SMSService did."""
    credential = await get_active_credential(db)
    if credential is None:
        return None
    return credential.provider, load_credentials(credential)
