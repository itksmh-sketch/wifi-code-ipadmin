"""Platform payment credentials: DB-backed, falling back to .env.

Mirrors `settings_service`'s read-through pattern — the table wins when an active
row exists, otherwise the PLATFORM_BILLING_PAYSTACK_* env vars are used. That
means this module changes no behaviour until a key is actually entered, and an
emergency key can still be dropped into .env if the table is unreachable or wrong.

Paystack-only by design. The table is multi-provider-capable, but nothing in the
platform-billing path speaks anything but Paystack yet, so the resolver is
deliberately concrete rather than dispatching through the PaymentProvider ABC.

NOTE: nothing consumes `resolve_paystack_keys` yet. `initiate_invoice_payment`
and the platform-billing webhook still read `settings` directly; switching them
over is Phase 4, deliberately after the webhook's amount/currency verification
lands in Phase 3.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.models import PlatformPaymentCredential
from src.utils.encryption import decrypt_secret

PAYSTACK = "paystack"


@dataclass(frozen=True)
class PaystackKeys:
    """Resolved platform Paystack keys, plus where they came from."""
    public_key: str
    secret_key: str
    webhook_secret: str
    # "db" or "env" — surfaced so the UI can say which is in force.
    source: str

    @property
    def is_configured(self) -> bool:
        """Usable for a charge. The webhook secret is checked separately by the
        webhook itself, which fails closed without it."""
        return bool(self.public_key.strip() and self.secret_key.strip())


async def get_active_credential(db: AsyncSession) -> Optional[PlatformPaymentCredential]:
    """The single active credential row, or None if the table has no active row."""
    return (
        await db.execute(
            select(PlatformPaymentCredential).where(PlatformPaymentCredential.is_active.is_(True))
        )
    ).scalar_one_or_none()


async def get_credential(db: AsyncSession, provider: str = PAYSTACK) -> Optional[PlatformPaymentCredential]:
    """The stored row for a provider, active or not."""
    return (
        await db.execute(
            select(PlatformPaymentCredential).where(PlatformPaymentCredential.provider == provider)
        )
    ).scalar_one_or_none()


def _env_keys() -> PaystackKeys:
    settings = get_settings()
    return PaystackKeys(
        public_key=settings.platform_billing_paystack_public_key or "",
        secret_key=settings.platform_billing_paystack_secret_key or "",
        webhook_secret=settings.platform_billing_paystack_webhook_secret or "",
        source="env",
    )


def keys_from_credential(credential: PlatformPaymentCredential) -> PaystackKeys:
    """Decrypt a stored row. Used for the row in force and, separately, to show
    what a deactivated row holds — a stored row is worth reporting even when it
    is not the one being used."""
    return PaystackKeys(
        public_key=decrypt_secret(credential.public_key_encrypted),
        secret_key=decrypt_secret(credential.secret_key_encrypted),
        webhook_secret=(
            decrypt_secret(credential.webhook_secret_encrypted)
            if credential.webhook_secret_encrypted
            else ""
        ),
        source="db",
    )


async def resolve_paystack_keys(db: AsyncSession) -> PaystackKeys:
    """Active DB row if there is one, otherwise the .env values."""
    credential = await get_active_credential(db)
    if credential is None or credential.provider != PAYSTACK:
        return _env_keys()
    return keys_from_credential(credential)
