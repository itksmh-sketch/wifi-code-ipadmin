"""Platform SMS credentials: the platform's own Arkesel keys for the
platform-provided SMS gateway.

Mirrors payment_credentials_service's read pattern, simplified: a single
active row (uq_platform_sms_credentials_one_active), no .env fallback — there
is no PLATFORM_SMS_* equivalent, so a platform-gateway send with no active row
here is simply refused rather than silently degraded. Uses the newer
single-Fernet-blob credential shape shared with OperatorSMSCredential, not
PlatformPaymentCredential's per-field encrypted columns.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import PlatformSMSCredential

ARKESEL = "arkesel"

__all__ = ["ARKESEL", "get_active_credential", "get_credential"]


async def get_active_credential(db: AsyncSession) -> Optional[PlatformSMSCredential]:
    """The single active platform SMS credential row, or None if unconfigured."""
    return (
        await db.execute(
            select(PlatformSMSCredential).where(PlatformSMSCredential.is_active.is_(True))
        )
    ).scalar_one_or_none()


async def get_credential(db: AsyncSession, provider: str = ARKESEL) -> Optional[PlatformSMSCredential]:
    """The stored row for a provider, active or not."""
    return (
        await db.execute(
            select(PlatformSMSCredential).where(PlatformSMSCredential.provider == provider)
        )
    ).scalar_one_or_none()
