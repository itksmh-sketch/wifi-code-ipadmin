"""Operator bring-your-own SMS-gateway credentials.

A thin adapter over the shared, catalog-schema-driven credentials core
(``src/modules/credentials/``) — the same five-endpoint CRUD as
``payment-credentials``. One row per provider (``operator_sms_credentials``,
keyed ``(isp_operator_id, provider)``), one active; the field list per provider
comes from its ``provider_catalog`` credential_schema.

SMS is not an onboarding-checklist step, so ``checklist_key`` is None. The
platform-gateway SMS option (``arkesel_platform``) is not operator-
configurable through the generic PUT below — its catalog row is
``configured_by: platform_admin``, which the shared core rejects with a 400.
It has its own dedicated ``/activate-platform`` endpoint instead: no
credentials to supply, just a selection marker.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import OperatorSMSCredential, ProviderCatalogEntry
from src.middleware.auth import TenantContext, get_admin_tenant_context
from src.modules.credentials.router import make_credentials_router
from src.modules.credentials.service import build_view, deactivate_others, dump_credentials
from src.modules.sms.providers.registry import build_sms_provider
from src.schemas import CredentialsView

router = make_credentials_router(
    category="sms",
    prefix="/sms-credentials",
    tags=["sms-credentials"],
    model=OperatorSMSCredential,
    build_provider=build_sms_provider,
    checklist_key=None,
)

PLATFORM_GATEWAY_PROVIDER_KEY = "arkesel_platform"


@router.post("/activate-platform", response_model=CredentialsView)
async def activate_platform_gateway(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    """Opt into the platform-provided Arkesel gateway.

    No credentials to supply — the row exists purely as the operator's
    selection marker (credentials_encrypted holds an empty placeholder blob,
    never read; resolve_active_sms_provider short-circuits to
    platform_sms_credentials for this provider instead). Reuses the same
    deactivate_others() helper and uq_operator_sms_credentials_one_active
    constraint as bring-your-own activation, so at most one SMS provider is
    ever active per operator — switching to the platform gateway atomically
    switches off any bring-your-own row, and vice versa.
    """
    entry = (
        await db.execute(
            select(ProviderCatalogEntry).where(
                ProviderCatalogEntry.category == "sms",
                ProviderCatalogEntry.provider_key == PLATFORM_GATEWAY_PROVIDER_KEY,
            )
        )
    ).scalar_one_or_none()
    if entry is None or not entry.is_available:
        raise HTTPException(status_code=404, detail="The platform SMS gateway is not currently offered.")

    row = (
        await db.execute(
            select(OperatorSMSCredential).where(
                OperatorSMSCredential.isp_operator_id == tenant.isp_operator_id,
                OperatorSMSCredential.provider == PLATFORM_GATEWAY_PROVIDER_KEY,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = OperatorSMSCredential(
            isp_operator_id=tenant.isp_operator_id,
            provider=PLATFORM_GATEWAY_PROVIDER_KEY,
            credentials_encrypted=dump_credentials({}),
            is_active=False,
        )
        db.add(row)
        await db.flush()

    await deactivate_others(db, OperatorSMSCredential, tenant.isp_operator_id, row.id)
    row.is_active = True
    await db.commit()
    return await build_view(db, category="sms", model=OperatorSMSCredential, operator_id=tenant.isp_operator_id)
