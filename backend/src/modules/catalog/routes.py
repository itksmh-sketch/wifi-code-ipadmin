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

# Which real vendor backs a platform-provided row (e.g. Arkesel, today) is
# deliberately never exposed to operators — display_name/description here are
# the platform admin's own, accurate labels (same DB row the platform-owner
# endpoint returns, since the admin needs to know which vendor they're
# actually managing), so this endpoint substitutes generic text for any
# is_platform_provided row rather than passing those two fields through.
_PLATFORM_GATEWAY_DISPLAY_NAME = "Platform SMS Gateway"
_PLATFORM_GATEWAY_DESCRIPTION = (
    "Send through the platform's own SMS gateway and get billed per segment "
    "on your monthly invoice. No credentials required."
)


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
    # sort_order and the catalog id stay server-side. platform_rate_per_segment is
    # the one internal field let through, and only for platform-provided rows —
    # it's the operator's actual cost, so they need it to decide whether to opt
    # in; bring-your-own rows never carry a rate here regardless of what's stored.
    return [
        {
            "provider_key": row.provider_key,
            "display_name": _PLATFORM_GATEWAY_DISPLAY_NAME if row.is_platform_provided else row.display_name,
            "description": _PLATFORM_GATEWAY_DESCRIPTION if row.is_platform_provided else row.description,
            # Only configured_by survives for a platform-provided row — the
            # frontend needs it to know there's no credentials form to render.
            # fields (which would otherwise name the vendor, e.g. "Arkesel API
            # key") never reach an operator; they never fill that form in.
            "credential_schema": (
                {"configured_by": (row.credential_schema or {}).get("configured_by")}
                if row.is_platform_provided
                else (row.credential_schema or {})
            ),
            "is_platform_provided": bool(row.is_platform_provided),
            "platform_rate_per_segment": (
                str(row.platform_rate_per_segment)
                if row.is_platform_provided and row.platform_rate_per_segment is not None
                else None
            ),
        }
        for row in rows
    ]
