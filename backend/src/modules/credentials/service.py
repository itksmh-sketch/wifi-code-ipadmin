"""Operator bring-your-own provider credentials — category-agnostic core.

An operator configures one row per provider (a table keyed
``(isp_operator_id, provider)``) and marks one active. Which fields a provider
needs comes from that provider's ``provider_catalog.credential_schema``, so there
is no per-provider code here.

The credential blob: ``<table>.credentials_encrypted`` is a Fernet token wrapping
``json.dumps({field_name: value}, sort_keys=True)``. Every read/write goes
through :func:`load_credentials` / :func:`dump_credentials` so the shape lives in
one place — same discipline as ``routers.nas_secret``.

Consumed by ``src/modules/credentials/router.py`` (the shared 5-endpoint CRUD)
and, for the payment side, re-exported by
``src/modules/payments/provider_resolver.py``. See
docs/payment-multi-provider-design.md.
"""
from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ISPOperator, ProviderCatalogEntry
from src.modules.webhooks.urls import build_webhook_url
from src.schemas import ConfiguredProviderView, CredentialsView
from src.utils.encryption import decrypt_secret, encrypt_secret


def load_credentials(row) -> dict:
    """Decrypt a credential row into its ``{field_name: value}`` dict."""
    return json.loads(decrypt_secret(row.credentials_encrypted))


def dump_credentials(values: dict) -> str:
    """Encrypt a ``{field_name: value}`` dict for storage. Sorted keys so a
    re-save with the same values produces a stable ciphertext-input."""
    return encrypt_secret(json.dumps(values, sort_keys=True, separators=(",", ":")))


def mask(value: str | None) -> str | None:
    if not value:
        return None
    return "••••" + value[-4:]


def schema_field_names(entry: ProviderCatalogEntry) -> list[str]:
    return [f["name"] for f in (entry.credential_schema or {}).get("fields", [])]


async def resolve_catalog_entry(
    db: AsyncSession, category: str, provider: str
) -> ProviderCatalogEntry:
    """The catalog row for an operator-configurable provider in this category.

    404 if unknown or not offered to operators; 400 if the provider's
    credentials are the platform's to configure, not the operator's.
    """
    entry = (
        await db.execute(
            select(ProviderCatalogEntry).where(
                ProviderCatalogEntry.category == category,
                ProviderCatalogEntry.provider_key == provider,
            )
        )
    ).scalar_one_or_none()
    if entry is None or not entry.is_available:
        raise HTTPException(status_code=404, detail=f"Unknown or unavailable {category} provider")
    if (entry.credential_schema or {}).get("configured_by") != "operator":
        raise HTTPException(
            status_code=400,
            detail=f"{entry.display_name} credentials are managed by the platform, not the operator.",
        )
    return entry


def validate_values(entry: ProviderCatalogEntry, values: dict[str, str]) -> dict[str, str]:
    fields = (entry.credential_schema or {}).get("fields", [])
    names = {f["name"] for f in fields}
    unknown = set(values) - names
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown field(s): {', '.join(sorted(unknown))}")
    cleaned: dict[str, str] = {}
    for f in fields:
        raw = values.get(f["name"])
        if f.get("required") and not (raw and raw.strip()):
            raise HTTPException(status_code=400, detail=f"{f['label']} is required")
        if raw and raw.strip():
            cleaned[f["name"]] = raw.strip()
    return cleaned


async def deactivate_others(db: AsyncSession, model, operator_id, keep_id) -> None:
    """Clear the partial-unique conflict BEFORE the kept row is set active."""
    await db.execute(
        update(model)
        .where(model.isp_operator_id == operator_id, model.id != keep_id)
        .values(is_active=False)
    )


async def build_view(
    db: AsyncSession, *, category: str, model, operator_id
) -> CredentialsView:
    rows = list(
        (
            await db.execute(
                select(model)
                .where(model.isp_operator_id == operator_id)
                .order_by(model.provider)
            )
        ).scalars().all()
    )
    # Field names come from the catalog schema so hints line up with the form.
    schemas = {
        e.provider_key: schema_field_names(e)
        for e in (
            await db.execute(
                select(ProviderCatalogEntry).where(ProviderCatalogEntry.category == category)
            )
        ).scalars().all()
    }
    slug = None
    if category == "payment" and rows:
        operator = await db.get(ISPOperator, operator_id)
        slug = operator.slug if operator else None

    configured: list[ConfiguredProviderView] = []
    active_provider = None
    for row in rows:
        if row.is_active:
            active_provider = row.provider
        stored = load_credentials(row)
        hints = {name: mask(stored.get(name)) for name in schemas.get(row.provider, list(stored))}
        configured.append(
            ConfiguredProviderView(
                provider=row.provider,
                is_active=bool(row.is_active),
                field_hints=hints,
                last_validated_at=row.last_validated_at,
                last_validation_error=row.last_validation_error,
                webhook_url=build_webhook_url(row.provider, slug) if category == "payment" else None,
            )
        )
    return CredentialsView(active_provider=active_provider, configured=configured)
