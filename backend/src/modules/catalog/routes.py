"""Operator-facing provider catalog.

The platform-owner endpoint (``GET /api/v1/platform/providers``) returns the full
catalog including internal state — is_integrated, the platform-gateway per-message
rate, catalog IDs. Operators must not see any of that. This endpoint returns only
the providers an operator can actually pick right now (``is_available = true``)
and only the fields the config UI needs.

One query param, ``category`` — the later SMS provider-selection feature reuses
this endpoint unchanged as ``?category=sms``.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import ProviderCatalogEntry
from src.middleware.auth import TenantContext, get_admin_tenant_context
from src.schemas import OperatorProviderResponse

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("", response_model=list[OperatorProviderResponse])
async def list_available_providers(
    category: Literal["payment", "sms"],
    db: AsyncSession = Depends(get_db),
    _: TenantContext = Depends(get_admin_tenant_context),
):
    """Available providers in one category, ordered for display.

    Catalog rows are platform-global, so the tenant context only gates access —
    its value is not used.
    """
    rows = (
        await db.execute(
            select(ProviderCatalogEntry)
            .where(
                ProviderCatalogEntry.category == category,
                ProviderCatalogEntry.is_available == True,  # noqa: E712
            )
            .order_by(ProviderCatalogEntry.sort_order, ProviderCatalogEntry.display_name)
        )
    ).scalars().all()

    # Explicit allow-list — never spread the ORM row. is_integrated, is_available,
    # platform_rate_per_message, sort_order and the catalog id stay server-side.
    return [
        {
            "provider_key": row.provider_key,
            "display_name": row.display_name,
            "description": row.description,
            "credential_schema": row.credential_schema or {},
            "is_platform_provided": bool(row.is_platform_provided),
        }
        for row in rows
    ]
