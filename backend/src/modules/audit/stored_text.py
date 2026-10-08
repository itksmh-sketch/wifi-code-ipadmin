"""Scrubbing router and provider text before it is stored outside audit_log.

Provision logs, CoA events, router setup status, credential test results,
payment failure reasons and SMS errors all keep text a router or provider
sent back, and several of them are readable by roles that may not see the
secrets (staff read provision logs). Each writer passes its text through
here first, with the secret values that call could have used.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    OperatorPaymentCredential,
    PlatformNotificationSMSCredential,
    PlatformPaymentCredential,
    PlatformSMSCredential,
    Router,
)
from src.modules.audit.redact import scrub, scrub_values
from src.modules.audit.router_secrets import router_secret_values
from src.modules.credentials.service import operator_credential_values, try_load_credentials


async def _router(db: AsyncSession, router: Router | uuid.UUID | str | None) -> Router | None:
    """A router row, or anything shaped like one; an id is loaded."""
    if router is None or not isinstance(router, (uuid.UUID, str)):
        return router
    return await db.get(Router, uuid.UUID(str(router)))


async def router_secrets(db: AsyncSession, router: Router | uuid.UUID | str | None) -> list[str]:
    row = await _router(db, router)
    return await router_secret_values(db, row) if row is not None else []


async def router_error(db: AsyncSession, router, text: Any) -> str | None:
    """An error from (or about) a router, scrubbed and capped."""
    if text is None:
        return None
    return scrub(text, await router_secrets(db, router))


async def router_commands(db: AsyncSession, router, commands: list[Any] | None) -> list[Any]:
    """A command list as stored on a provision log, every string scrubbed, uncapped."""
    if not commands:
        return list(commands or [])
    return scrub_values(list(commands), await router_secrets(db, router))


async def operator_payment_error(db: AsyncSession, operator_id, text: Any) -> str | None:
    if text is None:
        return None
    return scrub(text, await operator_credential_values(db, OperatorPaymentCredential, operator_id))


async def platform_secret_values(db: AsyncSession) -> list[str]:
    """Every platform payment and SMS credential value: stored rows, and the
    .env Paystack keys used when no row is active."""
    from src.modules.platform.payment_credentials_service import _env_keys, keys_from_credential

    values: list[str] = []
    for model in (PlatformSMSCredential, PlatformNotificationSMSCredential):
        for row in (await db.execute(select(model))).scalars().all():
            values += [str(v) for v in (try_load_credentials(row) or {}).values() if v]
    keys = []
    for row in (await db.execute(select(PlatformPaymentCredential))).scalars().all():
        try:
            keys.append(keys_from_credential(row))
        except Exception:
            pass
    try:
        keys.append(_env_keys())
    except Exception:
        pass
    for k in keys:
        values += [v for v in (k.public_key, k.secret_key, k.webhook_secret) if v]
    return values


async def platform_error(db: AsyncSession, text: Any, *extra: str | None) -> str | None:
    """A platform provider's error (credential test, SMS send), scrubbed of
    every platform credential value plus ``extra`` (e.g. .env keys in use)."""
    if text is None:
        return None
    return scrub(text, [*await platform_secret_values(db), *extra])
