"""Resolve which payment provider an operator sells through.

The encrypted-credential-blob serialisation (`load_credentials` /
`dump_credentials`) is category-agnostic and now lives in
`src.modules.credentials.service`; it is re-exported here so the many existing
payment/webhook call sites keep importing it from this module. See
docs/payment-multi-provider-design.md.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import OperatorPaymentCredential
from src.modules.credentials.service import dump_credentials, load_credentials

__all__ = ["load_credentials", "dump_credentials", "resolve_active_payment_provider"]


async def resolve_active_payment_provider(
    db: AsyncSession, operator_id: uuid.UUID
) -> tuple[str, dict]:
    """The operator's active payment provider: `(provider_key, credentials)`.

    The `uq_operator_payment_credentials_one_active` partial unique index
    guarantees at most one active row per operator, so this is a single lookup.
    Raises ValueError if the operator has configured none.
    """
    row = (
        await db.execute(
            select(OperatorPaymentCredential).where(
                OperatorPaymentCredential.isp_operator_id == operator_id,
                OperatorPaymentCredential.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError("Operator has not configured a payment provider")
    return row.provider, load_credentials(row)
