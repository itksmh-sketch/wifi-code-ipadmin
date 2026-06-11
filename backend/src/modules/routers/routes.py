import asyncio
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import Router, Site
from src.middleware.auth import TenantContext, get_admin_tenant_context, require_active_operator
from src.schemas import ErrorResponse, RouterCreate, RouterResponse, RouterUpdate
from src.utils.encryption import decrypt_secret, encrypt_secret
from src.utils.freeradius_reload import reload_freeradius_clients

router = APIRouter(prefix="/sites", tags=["routers"])


@router.get("/{site_id}/routers", response_model=List[RouterResponse])
async def list_routers(site_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    result = await db.execute(
        select(Router).where(Router.site_id == site_id, Router.isp_operator_id == tenant.isp_operator_id).order_by(Router.name)
    )
    routers = result.scalars().all()
    # Decrypt nas_secret for response
    for r in routers:
        try:
            r.nas_secret = decrypt_secret(r.nas_secret)
        except Exception:
            r.nas_secret = "****"
    return routers


@router.post("/{site_id}/routers", response_model=RouterResponse, status_code=201, responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def create_router(site_id: uuid.UUID, body: RouterCreate, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_active_operator)):
    site_result = await db.execute(select(Site).where(Site.id == site_id, Site.isp_operator_id == tenant.isp_operator_id))
    if not site_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Site not found")

    encrypted_secret = encrypt_secret(body.nas_secret)
    r = Router(
        isp_operator_id=tenant.isp_operator_id,
        site_id=site_id,
        name=body.name,
        ip_address=body.ip_address,
        nas_identifier=body.nas_identifier,
        nas_secret=encrypted_secret,
        nas_secret_plain=body.nas_secret,
        is_active=body.is_active,
    )
    db.add(r)
    await db.commit()
    await db.refresh(r)
    r.nas_secret = body.nas_secret  # return plaintext on creation
    from src.modules.onboarding import mark_checklist
    await mark_checklist(db, tenant.isp_operator_id, "router_added")
    await db.commit()
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, reload_freeradius_clients)
    return r


@router.put("/routers/{router_id}", response_model=RouterResponse, responses={404: {"model": ErrorResponse}})
async def update_router(router_id: uuid.UUID, body: RouterUpdate, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    result = await db.execute(select(Router).where(Router.id == router_id, Router.isp_operator_id == tenant.isp_operator_id))
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Router not found")

    if body.name is not None:
        r.name = body.name
    if body.ip_address is not None:
        r.ip_address = body.ip_address
    if body.nas_identifier is not None:
        r.nas_identifier = body.nas_identifier
    if body.nas_secret is not None:
        r.nas_secret = encrypt_secret(body.nas_secret)
        r.nas_secret_plain = body.nas_secret
    if body.is_active is not None:
        r.is_active = body.is_active

    await db.commit()
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, reload_freeradius_clients)
    await db.refresh(r)
    try:
        r.nas_secret = decrypt_secret(r.nas_secret)
    except Exception:
        r.nas_secret = "****"
    return r


@router.delete("/routers/{router_id}", status_code=204, responses={404: {"model": ErrorResponse}})
async def delete_router(router_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    result = await db.execute(select(Router).where(Router.id == router_id, Router.isp_operator_id == tenant.isp_operator_id))
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Router not found")
    # Remove the WireGuard peer from the interface before deleting the router.
    # The wg_ip_allocations row is removed by the ON DELETE CASCADE.
    if r.wg_peer_public_key:
        from src.modules.wireguard.service import WireGuardError, WireGuardService
        try:
            await WireGuardService().remove_peer(r.wg_peer_public_key)
        except WireGuardError:
            pass  # best-effort; never block router deletion on the sidecar
    await db.delete(r)
    await db.commit()
