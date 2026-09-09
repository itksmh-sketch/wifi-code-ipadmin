"""Resolve which payment provider an operator sells through, and the
serialisation of the encrypted credential blob.

`operator_payment_credentials.credentials_encrypted` is a Fernet token wrapping
`json.dumps({field_name: value}, sort_keys=True)`, keyed by the provider's
`provider_catalog.credential_schema` field names. Every read and write of that
column goes through `load_credentials` / `dump_credentials` so the shape stays in
one place. See docs/payment-multi-provider-design.md.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import OperatorPaymentCredential
from src.utils.encryption import decrypt_secret, encrypt_secret


def load_credentials(row: OperatorPaymentCredential) -> dict:
    """Decrypt a credential row into its `{field_name: value}` dict."""
    return json.loads(decrypt_secret(row.credentials_encrypted))


def dump_credentials(values: dict) -> str:
    """Encrypt a `{field_name: value}` dict for storage. Sorted keys so a
    re-save with the same values produces a stable ciphertext-input."""
    return encrypt_secret(json.dumps(values, sort_keys=True, separators=(",", ":")))


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
