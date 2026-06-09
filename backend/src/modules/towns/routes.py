from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from src.db.base import get_db
from src.db.models import Town, Site
from src.schemas import TownCreate, TownUpdate, TownResponse, SiteCreate, SiteResponse, ErrorResponse
from src.middleware.auth import TenantContext, get_admin_tenant_context, require_active_operator
from typing import List
import uuid

router = APIRouter(prefix="/towns", tags=["towns"])


@router.get("", response_model=List[TownResponse])
async def list_towns(db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    result = await db.execute(select(Town).where(Town.isp_operator_id == tenant.isp_operator_id).order_by(Town.name))
    return result.scalars().all()


@router.post("", response_model=TownResponse, status_code=201, responses={400: {"model": ErrorResponse}})
async def create_town(body: TownCreate, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_active_operator)):
    town = Town(isp_operator_id=tenant.isp_operator_id, name=body.name, region=body.region)
    db.add(town)
    await db.commit()
    await db.refresh(town)
    from src.modules.onboarding import mark_checklist
    await mark_checklist(db, tenant.isp_operator_id, "town_added")
    await db.commit()
    return town


@router.get("/{town_id}", response_model=TownResponse, responses={404: {"model": ErrorResponse}})
async def get_town(town_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    result = await db.execute(select(Town).where(Town.id == town_id, Town.isp_operator_id == tenant.isp_operator_id))
    town = result.scalar_one_or_none()
    if not town:
        raise HTTPException(status_code=404, detail="Town not found")
    return town


@router.put("/{town_id}", response_model=TownResponse, responses={404: {"model": ErrorResponse}})
async def update_town(town_id: uuid.UUID, body: TownUpdate, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    result = await db.execute(select(Town).where(Town.id == town_id, Town.isp_operator_id == tenant.isp_operator_id))
    town = result.scalar_one_or_none()
    if not town:
        raise HTTPException(status_code=404, detail="Town not found")
    if body.name is not None:
        town.name = body.name
    if body.region is not None:
        town.region = body.region
    await db.commit()
    await db.refresh(town)
    return town


@router.get("/{town_id}/sites", response_model=List[SiteResponse])
async def list_sites_in_town(town_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    result = await db.execute(
        select(Site).where(Site.town_id == town_id, Site.isp_operator_id == tenant.isp_operator_id).order_by(Site.name)
    )
    return result.scalars().all()


@router.post("/{town_id}/sites", response_model=SiteResponse, status_code=201, responses={404: {"model": ErrorResponse}})
async def create_site(town_id: uuid.UUID, body: SiteCreate, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_active_operator)):
    town_result = await db.execute(select(Town).where(Town.id == town_id, Town.isp_operator_id == tenant.isp_operator_id))
    town = town_result.scalar_one_or_none()
    if not town:
        raise HTTPException(status_code=404, detail="Town not found")
    site = Site(isp_operator_id=tenant.isp_operator_id, town_id=town_id, name=body.name, address=body.address)
    db.add(site)
    await db.commit()
    await db.refresh(site)
    return site
