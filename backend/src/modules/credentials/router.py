"""Factory for an operator bring-your-own-credentials router.

Both the payment (``/payment-credentials``) and SMS (``/sms-credentials``) APIs
are this same five-endpoint CRUD over a ``(isp_operator_id, provider)`` table,
differing only in category, the ORM model, how a provider object is built for
the test call, and which onboarding-checklist item a save completes.

See docs/payment-multi-provider-design.md.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import ProviderCatalogEntry
from src.middleware.auth import TenantContext, get_admin_tenant_context, require_active_operator, require_recent_pin
from src.middleware.permissions import SECRETS, require_perm
from src.modules import audit
from src.modules.audit.fields import credential_diff
from src.modules.audit.redact import scrub
from src.modules.credentials.service import (
    build_view,
    deactivate_others,
    dump_credentials,
    load_credentials,
    resolve_catalog_entry,
    secret_field_names,
    try_load_credentials,
    validate_values,
)
from src.schemas import CredentialsView, CredentialUpsert


def make_credentials_router(
    *,
    category: str,
    prefix: str,
    tags: list[str],
    model,
    build_provider: Callable[[str, dict], object],
    checklist_key: str | None,
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=tags)
    # Rows say "payment_credentials.set", "sms_credentials.test", ...: the
    # provider, which fields changed (secret ones only as "[redacted]") and the
    # active state. Never a key, secret, token or webhook-secret value.
    action = f"{category}_credentials"
    target_type = f"{category}_provider"
    label = {"payment": "payment", "sms": "SMS"}.get(category, category)

    async def _row(db: AsyncSession, operator_id, provider):
        return (
            await db.execute(
                select(model).where(
                    model.isp_operator_id == operator_id,
                    model.provider == provider,
                )
            )
        ).scalar_one_or_none()

    async def _view(db: AsyncSession, operator_id) -> CredentialsView:
        return await build_view(db, category=category, model=model, operator_id=operator_id)

    async def _active_others(db: AsyncSession, operator_id, keep_id) -> list[str]:
        """Providers that deactivate_others() is about to switch off."""
        return list(
            (
                await db.execute(
                    select(model.provider).where(
                        model.isp_operator_id == operator_id,
                        model.id != keep_id,
                        model.is_active == True,  # noqa: E712
                    )
                )
            ).scalars().all()
        )

    async def _catalog(db: AsyncSession, provider: str):
        return (
            await db.execute(
                select(ProviderCatalogEntry).where(
                    ProviderCatalogEntry.category == category,
                    ProviderCatalogEntry.provider_key == provider,
                )
            )
        ).scalar_one_or_none()

    async def _mark_configured(db: AsyncSession, operator_id) -> None:
        if not checklist_key:
            return
        from src.modules.onboarding import mark_checklist

        await mark_checklist(db, operator_id, checklist_key)

    @router.get("", response_model=CredentialsView, dependencies=[Depends(require_perm(SECRETS))])
    async def get_credentials(
        db: AsyncSession = Depends(get_db),
        tenant: TenantContext = Depends(get_admin_tenant_context),
    ):
        return await _view(db, tenant.isp_operator_id)

    @router.put(
        "/{provider}",
        response_model=CredentialsView,
        dependencies=[Depends(audit.audited), Depends(require_perm(SECRETS)), Depends(require_recent_pin)],
    )
    async def upsert_credentials(
        provider: str,
        body: CredentialUpsert,
        db: AsyncSession = Depends(get_db),
        # Suspension-gated: storing and activating a provider is re-arming the
        # till. `test` and `delete` below stay open — a diagnostic changes
        # nothing and removing a provider only reduces capability.
        tenant: TenantContext = Depends(require_active_operator),
    ):
        entry = await resolve_catalog_entry(db, category, provider)
        cleaned = validate_values(entry, body.values)

        row = await _row(db, tenant.isp_operator_id, provider)
        existing_active = (
            await db.execute(
                select(model.id).where(
                    model.isp_operator_id == tenant.isp_operator_id,
                    model.is_active == True,  # noqa: E712
                )
            )
        ).scalar_one_or_none()

        if body.activate is True:
            make_active = True
        elif body.activate is False:
            make_active = False
        else:
            # Default: activate only if the operator has no active provider, or
            # this row is already the active one.
            make_active = existing_active is None or (row is not None and row.id == existing_active)

        created = row is None
        before_values = try_load_credentials(row)
        was_active = bool(row.is_active) if row is not None else False
        if row is None:
            row = model(
                isp_operator_id=tenant.isp_operator_id,
                provider=provider,
                credentials_encrypted=dump_credentials(cleaned),
                is_active=False,
            )
            db.add(row)
            await db.flush()
        else:
            # Fernet output differs on every call: keep the stored blob when the
            # values are the same, so nothing reads as changed that wasn't.
            if before_values != cleaned:
                row.credentials_encrypted = dump_credentials(cleaned)
            row.updated_at = datetime.now(timezone.utc)
            # Re-entered keys invalidate the previous test result.
            row.last_validated_at = None
            row.last_validation_error = None

        switched_off: list[str] = []
        if make_active:
            switched_off = await _active_others(db, tenant.isp_operator_id, row.id)
            await deactivate_others(db, model, tenant.isp_operator_id, row.id)
            row.is_active = True
        else:
            row.is_active = False

        changes = credential_diff(before_values, cleaned, secret_fields=secret_field_names(entry)) or {}
        if was_active != row.is_active or created:
            changes["is_active"] = {"before": None if created else was_active, "after": row.is_active}
        if changes or switched_off:
            verb = "Added" if created else "Updated"
            audit.record(
                db, f"{action}.set", target_type=target_type, target_id=provider,
                summary=f"{verb} {label} credentials for {provider}" + (" (active)" if row.is_active else ""),
                changes=changes or None,
                detail={"provider": provider, "created": created, "is_active": row.is_active, "deactivated": switched_off},
            )

        await db.commit()
        if row.is_active:
            await _mark_configured(db, tenant.isp_operator_id)
            await db.commit()
        return await _view(db, tenant.isp_operator_id)

    @router.post(
        "/{provider}/activate",
        response_model=CredentialsView,
        dependencies=[Depends(audit.audited), Depends(require_perm(SECRETS)), Depends(require_recent_pin)],
    )
    async def activate_provider(
        provider: str,
        db: AsyncSession = Depends(get_db),
        tenant: TenantContext = Depends(require_active_operator),
    ):
        row = await _row(db, tenant.isp_operator_id, provider)
        if row is None:
            raise HTTPException(status_code=404, detail="This provider is not configured")
        was_active = bool(row.is_active)
        switched_off = await _active_others(db, tenant.isp_operator_id, row.id)
        await deactivate_others(db, model, tenant.isp_operator_id, row.id)
        row.is_active = True
        if not was_active or switched_off:
            audit.record(
                db, f"{action}.activate", target_type=target_type, target_id=provider,
                summary=f"Made {provider} the active {label} provider",
                changes={"is_active": {"before": was_active, "after": True}} if not was_active else None,
                detail={"provider": provider, "is_active": True, "deactivated": switched_off},
            )
        await db.commit()
        await _mark_configured(db, tenant.isp_operator_id)
        await db.commit()
        return await _view(db, tenant.isp_operator_id)

    @router.post("/{provider}/test", response_model=CredentialsView, dependencies=[Depends(audit.audited), Depends(require_perm(SECRETS))])
    async def test_credentials(
        provider: str,
        db: AsyncSession = Depends(get_db),
        tenant: TenantContext = Depends(get_admin_tenant_context),
    ):
        row = await _row(db, tenant.isp_operator_id, provider)
        if row is None:
            raise HTTPException(status_code=404, detail="This provider is not configured")

        submitted = load_credentials(row)
        provider_obj = build_provider(provider, submitted)
        try:
            detail = await provider_obj.verify_credentials()
        except NotImplementedError as exc:
            raise HTTPException(
                status_code=400, detail="Test connection is not available for this provider."
            ) from exc
        except Exception as exc:  # noqa: BLE001 — the provider's message is the useful bit
            # A provider may echo the key it rejected ("Invalid key sk_live_…"):
            # the stored error, the row and the response all get the scrubbed text.
            error = scrub(exc, submitted.values()) or "Test failed"
            row.last_validation_error = error
            row.last_validated_at = None
            audit.record(
                db, f"{action}.test", target_type=target_type, target_id=provider,
                summary=f"Tested {label} credentials for {provider}: failed",
                detail={"provider": provider, "outcome": "failed", "error": error},
            )
            await db.commit()
            raise HTTPException(status_code=400, detail=error) from exc
        row.last_validated_at = datetime.now(timezone.utc)
        row.last_validation_error = None
        audit.record(
            db, f"{action}.test", target_type=target_type, target_id=provider,
            summary=f"Tested {label} credentials for {provider}: ok",
            detail={"provider": provider, "outcome": "ok"},
        )
        await db.commit()
        view = await _view(db, tenant.isp_operator_id)
        # Transient: a balance/account line from verify_credentials(), shown once
        # in the "Connection verified" message. None for providers with no such
        # concept (Paystack, Flutterwave) — the contract is unchanged for them.
        view.test_detail = detail if isinstance(detail, str) and detail.strip() else None
        return view

    @router.delete(
        "/{provider}",
        response_model=CredentialsView,
        dependencies=[Depends(audit.audited), Depends(require_perm(SECRETS)), Depends(require_recent_pin)],
    )
    async def delete_credentials(
        provider: str,
        db: AsyncSession = Depends(get_db),
        tenant: TenantContext = Depends(get_admin_tenant_context),
    ):
        row = await _row(db, tenant.isp_operator_id, provider)
        if row is None:
            raise HTTPException(status_code=404, detail="This provider is not configured")
        if row.is_active:
            raise HTTPException(
                status_code=409,
                detail="Cannot delete the active provider — switch to another provider first.",
            )
        entry = await _catalog(db, provider)
        audit.record(
            db, f"{action}.delete", target_type=target_type, target_id=provider,
            summary=f"Deleted {label} credentials for {provider}",
            changes=credential_diff(try_load_credentials(row), None, secret_fields=secret_field_names(entry)),
            detail={"provider": provider, "is_active": False},
        )
        await db.delete(row)
        await db.commit()
        return await _view(db, tenant.isp_operator_id)

    return router
