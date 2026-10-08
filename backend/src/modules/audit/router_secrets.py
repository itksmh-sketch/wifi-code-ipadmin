"""The secret values a router call may have used, for ``redact.scrub``.

An error string from RouterOS, the RADIUS apply or the VPN sidecar is stored
only after these are taken out of it: the NAS secret (stored and decrypted),
the API password, and both WireGuard keys.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Router, RouterCredential
from src.utils.encryption import decrypt_secret

logger = logging.getLogger("audit")


def _decrypted(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return decrypt_secret(value)
    except Exception:  # an undecryptable value can't have been sent anywhere
        return None


async def router_secret_values(db: AsyncSession, router_row: Router) -> list[str]:
    """Read before anything clears them: a tunnel removal blanks the keys."""
    values = [
        router_row.nas_secret_plain,
        _decrypted(router_row.nas_secret),
        router_row.nas_secret,
        router_row.wg_peer_public_key,
        _decrypted(router_row.wg_peer_private_key_encrypted),
        router_row.wg_peer_private_key_encrypted,
    ]
    try:
        encrypted = (
            await db.execute(select(RouterCredential.api_password_encrypted).where(RouterCredential.router_id == router_row.id))
        ).scalar_one_or_none()
    except Exception as exc:
        logger.error("audit_router_secrets_unreadable router_id=%s error=%s", router_row.id, exc)
        encrypted = None
    values += [encrypted, _decrypted(encrypted)]
    return [v for v in values if v]
