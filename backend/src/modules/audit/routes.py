"""The two audit-log read endpoints. All the rules live in audit.query.

GET /api/v1/admin/audit      an operator's own trail; superadmin only (AUDIT).
GET /api/v1/platform/audit   every operator's, and the platform's own.

Reading the log writes no row (it would only record the page being read).
"""
from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import PlatformOwner
from src.middleware.auth import TenantContext, get_platform_owner_context
from src.middleware.permissions import AUDIT, require_perm
from src.modules.audit.context import OPERATOR_USER, PLATFORM_OWNER, SYSTEM
from src.modules.audit.query import PAGE_DEFAULT, PAGE_MAX, BadQuery, Filters, list_rows
from src.modules.audit.read import VIEWER_OPERATOR, VIEWER_PLATFORM

admin_router = APIRouter(prefix="/admin/audit", tags=["audit"])
platform_router = APIRouter(prefix="/platform/audit", tags=["audit"])

# The operator page's "user" filter: one of the operator's admins (an id), or
# every platform action, or (with system rows on) every system action.
_USER_ALIASES = {"platform": PLATFORM_OWNER, "system": SYSTEM}


def _uuid_or_400(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {what}.")


async def _page(db, *, viewer, filters, tenant_id=None, cursor, limit):
    try:
        return await list_rows(db, viewer=viewer, filters=filters, tenant_id=tenant_id, cursor=cursor, limit=limit)
    except BadQuery as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@admin_router.get("")
async def operator_audit_log(
    user: str | None = Query(None, max_length=64, description="An admin user id, or 'platform' / 'system'"),
    action: str | None = Query(None, max_length=64, description="Action prefix, e.g. 'voucher' or 'plan.update'"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    include_system: bool = Query(False),
    cursor: str | None = Query(None, max_length=512),
    limit: int = Query(PAGE_DEFAULT, ge=1, le=PAGE_MAX),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_perm(AUDIT)),
):
    actor_type, actor_id = None, None
    if user in _USER_ALIASES:
        actor_type = _USER_ALIASES[user]
    elif user:
        actor_type, actor_id = OPERATOR_USER, _uuid_or_400(user, "user")
    filters = Filters(actor_type=actor_type, actor_id=actor_id, action_prefix=action, date_from=date_from,
                      date_to=date_to, include_system=include_system)
    return await _page(db, viewer=VIEWER_OPERATOR, filters=filters, tenant_id=tenant.isp_operator_id,
                       cursor=cursor, limit=limit)


@platform_router.get("")
async def platform_audit_log(
    operator: str | None = Query(None, max_length=64, description="An operator id, or 'none' for platform-wide rows"),
    actor_type: str | None = Query(None, max_length=32),
    action: str | None = Query(None, max_length=64),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    include_system: bool = Query(False),
    cursor: str | None = Query(None, max_length=512),
    limit: int = Query(PAGE_DEFAULT, ge=1, le=PAGE_MAX),
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    filters = Filters(
        operator_id=_uuid_or_400(operator, "operator") if operator and operator != "none" else None,
        platform_wide=operator == "none",
        actor_type=actor_type or None, action_prefix=action, date_from=date_from, date_to=date_to,
        include_system=include_system,
    )
    return await _page(db, viewer=VIEWER_PLATFORM, filters=filters, cursor=cursor, limit=limit)
