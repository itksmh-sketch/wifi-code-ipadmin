"""Resolve which SMS gateway an operator delivers voucher codes through.

Mirrors ``payments.provider_resolver`` — except a missing configuration is not an
error. It returns ``None`` and the caller (voucher delivery) silently skips,
matching today's ``sms_provider=''`` no-op. The credential-blob (de)serialisation
is the shared ``credentials.service.{load,dump}_credentials``.

``arkesel_platform`` is the one provider value in ``operator_sms_provider``
that isn't operator credentials at all — it's the opt-in marker for the
platform-provided gateway (row created with an empty placeholder
credentials_encrypted blob by the dedicated ``/activate-platform`` endpoint,
never through the generic bring-your-own PUT). Resolving it means reading
``platform_sms_credentials`` instead of decrypting the marker row itself.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import OperatorSMSCredential
from src.modules.credentials.service import load_credentials

__all__ = ["resolve_active_sms_provider"]

logger = logging.getLogger("sms.provider_resolver")

PLATFORM_GATEWAY_PROVIDER_KEY = "arkesel_platform"


async def resolve_active_sms_provider(
    db: AsyncSession, operator_id: uuid.UUID
) -> tuple[str, dict] | None:
    """The operator's active SMS provider: ``(provider_key, credentials)``, or
    ``None`` if they have configured none.

    The ``uq_operator_sms_credentials_one_active`` partial unique index
    guarantees at most one active row per operator, so this is a single lookup.
    """
    row = (
        await db.execute(
            select(OperatorSMSCredential).where(
                OperatorSMSCredential.isp_operator_id == operator_id,
                OperatorSMSCredential.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.provider == PLATFORM_GATEWAY_PROVIDER_KEY:
        from src.modules.platform import platform_sms_credentials_service as platform_creds

        credential = await platform_creds.get_active_credential(db)
        if credential is None:
            # Operator opted in before (or after) the platform admin stored a
            # working credential. Fail closed, same as "no active provider" —
            # but loud, since this is a misconfiguration, not a normal no-op.
            logger.error(
                "platform_sms_gateway_selected_but_not_configured operator=%s", operator_id
            )
            return None
        return row.provider, load_credentials(credential)
    return row.provider, load_credentials(row)
