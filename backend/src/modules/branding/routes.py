"""Operator-scoped captive-portal branding: read/update settings and logo upload.

Tenant isolation mirrors the other operator settings endpoints (e.g. payment
credentials): every query/mutation is keyed on ``tenant.isp_operator_id`` from
the authenticated admin context, so an operator can only ever touch its own row.
"""
from __future__ import annotations

import glob
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import ISPOperator
from src.middleware.auth import TenantContext, get_admin_tenant_context, require_active_operator
from src.modules.branding.service import build_branding
from src.portal.routes import PORTAL_DIR
from src.schemas import BrandingResponse, BrandingUpdate

router = APIRouter(prefix="/admin/branding", tags=["branding"])

# Logos live under the portal statics tree so they're served by the existing
# GET /portal/statics/{path} route (and persisted via the ./portal volume).
_LOGO_DIR = os.path.join(PORTAL_DIR, "statics", "branding")
_LOGO_URL_PREFIX = "/portal/statics/branding"
_MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2 MB
# content-type -> extension for the formats we accept. SVG is intentionally
# excluded: it can embed scripts and would be served same-origin.
_ALLOWED_LOGO_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}


async def _get_operator(db: AsyncSession, operator_id) -> ISPOperator:
    operator = (
        await db.execute(select(ISPOperator).where(ISPOperator.id == operator_id))
    ).scalar_one_or_none()
    if operator is None:
        raise HTTPException(status_code=404, detail="Operator not found")
    return operator


@router.get("", response_model=BrandingResponse)
async def get_branding(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    return build_branding(await _get_operator(db, tenant.isp_operator_id))


@router.put("", response_model=BrandingResponse)
async def update_branding(
    body: BrandingUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_active_operator),
):
    operator = await _get_operator(db, tenant.isp_operator_id)
    # Only fields present in the request are touched; an explicit null clears a
    # field back to the platform default (build_branding fills it on read).
    fields = body.model_dump(exclude_unset=True)
    for attr in ("portal_display_name", "primary_color", "accent_color", "background_gradient_start", "portal_welcome_message"):
        if attr in fields:
            setattr(operator, attr, fields[attr])
    operator.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(operator)
    return build_branding(operator)


@router.post("/logo", response_model=BrandingResponse)
async def upload_logo(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_active_operator),
):
    ext = _ALLOWED_LOGO_TYPES.get((file.content_type or "").lower())
    if ext is None:
        raise HTTPException(status_code=400, detail="Logo must be a PNG, JPG or WebP image")

    contents = await file.read()
    if len(contents) > _MAX_LOGO_BYTES:
        raise HTTPException(status_code=400, detail="Logo must be 2 MB or smaller")
    if not contents:
        raise HTTPException(status_code=400, detail="Logo file is empty")

    operator = await _get_operator(db, tenant.isp_operator_id)

    os.makedirs(_LOGO_DIR, exist_ok=True)
    # Remove any previous logo for this operator (any extension) so we don't leave
    # orphans, then write a fresh timestamped name to bust client/CDN caches.
    for stale in glob.glob(os.path.join(_LOGO_DIR, f"logo_{operator.id}_*")):
        try:
            os.remove(stale)
        except OSError:
            pass

    stamp = int(datetime.now(timezone.utc).timestamp())
    filename = f"logo_{operator.id}_{stamp}.{ext}"
    with open(os.path.join(_LOGO_DIR, filename), "wb") as fh:
        fh.write(contents)

    operator.logo_url = f"{_LOGO_URL_PREFIX}/{filename}"
    operator.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(operator)
    return build_branding(operator)
