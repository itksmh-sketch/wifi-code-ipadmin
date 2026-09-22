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
from src.middleware.auth import TenantContext, get_admin_tenant_context, require_active_operator, require_recent_pin
from src.modules.credentials.service import (
    build_view,
    deactivate_others,
    dump_credentials,
    load_credentials,
    resolve_catalog_entry,
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

    async def _mark_configured(db: AsyncSession, operator_id) -> None:
        if not checklist_key:
            return
        from src.modules.onboarding import mark_checklist

        await mark_checklist(db, operator_id, checklist_key)

    @router.get("", response_model=CredentialsView)
    async def get_credentials(
        db: AsyncSession = Depends(get_db),
        tenant: TenantContext = Depends(get_admin_tenant_context),
    ):
        return await _view(db, tenant.isp_operator_id)

    @router.put(
        "/{provider}",
        response_model=CredentialsView,
        dependencies=[Depends(require_recent_pin)],
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
            row.credentials_encrypted = dump_credentials(cleaned)
            row.updated_at = datetime.now(timezone.utc)
            # Re-entered keys invalidate the previous test result.
            row.last_validated_at = None
            row.last_validation_error = None

        if make_active:
            await deactivate_others(db, model, tenant.isp_operator_id, row.id)
            row.is_active = True
        else:
            row.is_active = False

        await db.commit()
        if row.is_active:
            await _mark_configured(db, tenant.isp_operator_id)
            await db.commit()
        return await _view(db, tenant.isp_operator_id)

    @router.post(
        "/{provider}/activate",
        response_model=CredentialsView,
        dependencies=[Depends(require_recent_pin)],
    )
    async def activate_provider(
        provider: str,
        db: AsyncSession = Depends(get_db),
        tenant: TenantContext = Depends(require_active_operator),
    ):
        row = await _row(db, tenant.isp_operator_id, provider)
        if row is None:
            raise HTTPException(status_code=404, detail="This provider is not configured")
        await deactivate_others(db, model, tenant.isp_operator_id, row.id)
        row.is_active = True
        await db.commit()
        await _mark_configured(db, tenant.isp_operator_id)
        await db.commit()
        return await _view(db, tenant.isp_operator_id)

    @router.post("/{provider}/test", response_model=CredentialsView)
    async def test_credentials(
        provider: str,
        db: AsyncSession = Depends(get_db),
        tenant: TenantContext = Depends(get_admin_tenant_context),
    ):
        row = await _row(db, tenant.isp_operator_id, provider)
        if row is None:
            raise HTTPException(status_code=404, detail="This provider is not configured")

        provider_obj = build_provider(provider, load_credentials(row))
        try:
            detail = await provider_obj.verify_credentials()
        except NotImplementedError as exc:
            raise HTTPException(
                status_code=400, detail="Test connection is not available for this provider."
            ) from exc
        except Exception as exc:  # noqa: BLE001 — the provider's message is the useful bit
            row.last_validation_error = str(exc)
            row.last_validated_at = None
            await db.commit()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        row.last_validated_at = datetime.now(timezone.utc)
        row.last_validation_error = None
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
        dependencies=[Depends(require_recent_pin)],
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
        await db.delete(row)
        await db.commit()
        return await _view(db, tenant.isp_operator_id)

    return router
