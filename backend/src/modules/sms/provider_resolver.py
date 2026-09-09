"""Resolve which SMS gateway an operator delivers voucher codes through.

Mirrors ``payments.provider_resolver`` — except a missing configuration is not an
error. It returns ``None`` and the caller (voucher delivery) silently skips,
matching today's ``sms_provider=''`` no-op. The credential-blob (de)serialisation
is the shared ``credentials.service.{load,dump}_credentials``.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import OperatorSMSCredential
from src.modules.credentials.service import load_credentials

__all__ = ["resolve_active_sms_provider"]


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
    return row.provider, load_credentials(row)
