"""Operator bring-your-own payment credentials — generic, catalog-schema driven.

An operator configures one row per provider (``operator_payment_credentials``,
keyed ``(isp_operator_id, provider)``) and marks one active. Which fields a
provider needs comes from that provider's ``provider_catalog`` credential_schema,
so there is no per-provider code here. See docs/payment-multi-provider-design.md.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import OperatorPaymentCredential, ProviderCatalogEntry
from src.middleware.auth import TenantContext, get_admin_tenant_context
from src.modules.payments.provider_resolver import dump_credentials, load_credentials
from src.modules.payments.providers.registry import build_payment_provider
from src.schemas import ConfiguredProviderView, PaymentCredentialsView, PaymentCredentialUpsert

router = APIRouter(prefix="/payment-credentials", tags=["payment-credentials"])


def _mask(value: str | None) -> str | None:
    if not value:
        return None
    return "••••" + value[-4:]


async def _payment_schema(db: AsyncSession, provider: str) -> ProviderCatalogEntry:
    """The catalog row for an operator-configurable payment provider, or 404."""
    entry = (
        await db.execute(
            select(ProviderCatalogEntry).where(
                ProviderCatalogEntry.category == "payment",
                ProviderCatalogEntry.provider_key == provider,
            )
        )
    ).scalar_one_or_none()
    if entry is None or not entry.is_available:
        raise HTTPException(status_code=404, detail="Unknown or unavailable payment provider")
    if (entry.credential_schema or {}).get("configured_by") != "operator":
        raise HTTPException(
            status_code=400,
            detail=f"{entry.display_name} credentials are managed by the platform, not the operator.",
        )
    return entry


def _schema_field_names(entry: ProviderCatalogEntry) -> list[str]:
    return [f["name"] for f in (entry.credential_schema or {}).get("fields", [])]


def _validate_values(entry: ProviderCatalogEntry, values: dict[str, str]) -> dict[str, str]:
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


async def _rows_for_operator(db: AsyncSession, operator_id) -> list[OperatorPaymentCredential]:
    return list(
        (
            await db.execute(
                select(OperatorPaymentCredential)
                .where(OperatorPaymentCredential.isp_operator_id == operator_id)
                .order_by(OperatorPaymentCredential.provider)
            )
        ).scalars().all()
    )


async def _view(db: AsyncSession, operator_id) -> PaymentCredentialsView:
    rows = await _rows_for_operator(db, operator_id)
    # Field names come from the catalog schema so hints line up with the form.
    schemas = {
        e.provider_key: _schema_field_names(e)
        for e in (
            await db.execute(
                select(ProviderCatalogEntry).where(ProviderCatalogEntry.category == "payment")
            )
        ).scalars().all()
    }
    configured: list[ConfiguredProviderView] = []
    active_provider = None
    for row in rows:
        if row.is_active:
            active_provider = row.provider
        stored = load_credentials(row)
        hints = {name: _mask(stored.get(name)) for name in schemas.get(row.provider, list(stored))}
        configured.append(
            ConfiguredProviderView(
                provider=row.provider,
                is_active=bool(row.is_active),
                field_hints=hints,
                last_validated_at=row.last_validated_at,
                last_validation_error=row.last_validation_error,
            )
        )
    return PaymentCredentialsView(active_provider=active_provider, configured=configured)


async def _deactivate_others(db: AsyncSession, operator_id, keep_id) -> None:
    # Clears the partial-unique conflict BEFORE the kept row is set active.
    await db.execute(
        update(OperatorPaymentCredential)
        .where(
            OperatorPaymentCredential.isp_operator_id == operator_id,
            OperatorPaymentCredential.id != keep_id,
        )
        .values(is_active=False)
    )


async def _mark_configured(db: AsyncSession, operator_id) -> None:
    from src.modules.onboarding import mark_checklist

    await mark_checklist(db, operator_id, "payment_configured")


@router.get("", response_model=PaymentCredentialsView)
async def get_payment_credentials(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    return await _view(db, tenant.isp_operator_id)


@router.put("/{provider}", response_model=PaymentCredentialsView)
async def upsert_payment_credentials(
    provider: str,
    body: PaymentCredentialUpsert,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    entry = await _payment_schema(db, provider)
    cleaned = _validate_values(entry, body.values)

    row = (
        await db.execute(
            select(OperatorPaymentCredential).where(
                OperatorPaymentCredential.isp_operator_id == tenant.isp_operator_id,
                OperatorPaymentCredential.provider == provider,
            )
        )
    ).scalar_one_or_none()

    existing_active = (
        await db.execute(
            select(OperatorPaymentCredential.id).where(
                OperatorPaymentCredential.isp_operator_id == tenant.isp_operator_id,
                OperatorPaymentCredential.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()

    if body.activate is True:
        make_active = True
    elif body.activate is False:
        make_active = False
    else:
        # Default: activate only if the operator has no active provider, or this
        # row is already the active one.
        make_active = existing_active is None or (row is not None and row.id == existing_active)

    if row is None:
        row = OperatorPaymentCredential(
            isp_operator_id=tenant.isp_operator_id,
            provider=provider,
            credentials_encrypted=dump_credentials(cleaned),
            is_active=False,
        )
        db.add(row)
        await db.flush()
    else:
        row.credentials_encrypted = dump_credentials(cleaned)
        row.updated_at = datetime.now(timezone.utc)
        # Re-entered keys invalidate the previous test result.
        row.last_validated_at = None
        row.last_validation_error = None

    if make_active:
        await _deactivate_others(db, tenant.isp_operator_id, row.id)
        row.is_active = True
    else:
        row.is_active = False

    await db.commit()
    if row.is_active:
        await _mark_configured(db, tenant.isp_operator_id)
        await db.commit()
    return await _view(db, tenant.isp_operator_id)


@router.post("/{provider}/activate", response_model=PaymentCredentialsView)
async def activate_payment_provider(
    provider: str,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    row = (
        await db.execute(
            select(OperatorPaymentCredential).where(
                OperatorPaymentCredential.isp_operator_id == tenant.isp_operator_id,
                OperatorPaymentCredential.provider == provider,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="This provider is not configured")
    await _deactivate_others(db, tenant.isp_operator_id, row.id)
    row.is_active = True
    await db.commit()
    await _mark_configured(db, tenant.isp_operator_id)
    await db.commit()
    return await _view(db, tenant.isp_operator_id)


@router.post("/{provider}/test", response_model=PaymentCredentialsView)
async def test_payment_credentials(
    provider: str,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    row = (
        await db.execute(
            select(OperatorPaymentCredential).where(
                OperatorPaymentCredential.isp_operator_id == tenant.isp_operator_id,
                OperatorPaymentCredential.provider == provider,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="This provider is not configured")

    provider_obj = build_payment_provider(provider, load_credentials(row), callback_url=None)
    try:
        await provider_obj.verify_credentials()
    except Exception as exc:  # noqa: BLE001 — the provider's message is the useful bit
        row.last_validation_error = str(exc)
        row.last_validated_at = None
        await db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row.last_validated_at = datetime.now(timezone.utc)
    row.last_validation_error = None
    await db.commit()
    return await _view(db, tenant.isp_operator_id)


@router.delete("/{provider}", response_model=PaymentCredentialsView)
async def delete_payment_credentials(
    provider: str,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    row = (
        await db.execute(
            select(OperatorPaymentCredential).where(
                OperatorPaymentCredential.isp_operator_id == tenant.isp_operator_id,
                OperatorPaymentCredential.provider == provider,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="This provider is not configured")
    if row.is_active:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete the active provider — switch to another provider first.",
        )
    await db.delete(row)
    await db.commit()
    return await _view(db, tenant.isp_operator_id)
