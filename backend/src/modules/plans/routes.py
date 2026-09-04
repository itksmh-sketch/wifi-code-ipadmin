from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from src.db.base import get_db
from src.db.models import Plan, Site
from src.schemas import PlanCreate, PlanUpdate, PlanResponse, ErrorResponse
from src.middleware.auth import TenantContext, get_admin_tenant_context, require_active_operator
from src.modules.plans.dedup import DUPLICATE_PLAN_MESSAGE, SETTINGS_FIELDS, find_duplicate_plan
from typing import List
import uuid

router = APIRouter(prefix="/plans", tags=["plans"])


@router.get("", response_model=List[PlanResponse])
async def list_plans(db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    result = await db.execute(select(Plan).where(Plan.isp_operator_id == tenant.isp_operator_id).order_by(Plan.name))
    return result.scalars().all()


@router.post("", response_model=PlanResponse, status_code=201, responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
async def create_plan(body: PlanCreate, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_active_operator)):
    if body.type == "time" and body.duration_minutes is None:
        raise HTTPException(status_code=400, detail="duration_minutes is required for time-based plans")
    if body.type == "data" and body.data_limit_mb is None:
        raise HTTPException(status_code=400, detail="data_limit_mb is required for data-based plans")

    if body.site_id is not None:
        site = (
            await db.execute(select(Site).where(Site.id == body.site_id, Site.isp_operator_id == tenant.isp_operator_id))
        ).scalar_one_or_none()
        if not site:
            raise HTTPException(status_code=404, detail="Site not found")

    # Dedup on settings, not name: two plans that would behave identically for the
    # same operator and site are the same package under a different label.
    duplicate = await find_duplicate_plan(
        db,
        tenant.isp_operator_id,
        site_id=body.site_id,
        plan_type=body.type.value,
        duration_minutes=body.duration_minutes,
        data_limit_mb=body.data_limit_mb,
        download_speed_kbps=body.download_speed_kbps,
        upload_speed_kbps=body.upload_speed_kbps,
        price_ghs=body.price_ghs,
    )
    if duplicate:
        raise HTTPException(status_code=409, detail=f'{DUPLICATE_PLAN_MESSAGE} ("{duplicate.name}")')

    plan = Plan(
        isp_operator_id=tenant.isp_operator_id,
        site_id=body.site_id,
        name=body.name,
        type=body.type.value,
        duration_minutes=body.duration_minutes,
        data_limit_mb=body.data_limit_mb,
        download_speed_kbps=body.download_speed_kbps,
        upload_speed_kbps=body.upload_speed_kbps,
        price_ghs=body.price_ghs,
        is_active=body.is_active,
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return plan


@router.put("/{plan_id}", response_model=PlanResponse, responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
async def update_plan(plan_id: uuid.UUID, body: PlanUpdate, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    result = await db.execute(select(Plan).where(Plan.id == plan_id, Plan.isp_operator_id == tenant.isp_operator_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    updates = body.model_dump(exclude_unset=True)

    # Same guard as creation, against the post-edit values -- otherwise a plan can
    # be edited into an exact copy of another one through the back door. Only runs
    # when the edit actually touches a settings field: a rename or an enable/disable
    # toggle must keep working even for duplicate pairs that predate this check.
    if SETTINGS_FIELDS & updates.keys():
        new_type = updates.get("type", plan.type)
        duplicate = await find_duplicate_plan(
            db,
            tenant.isp_operator_id,
            exclude_plan_id=plan.id,
            site_id=plan.site_id,
            plan_type=getattr(new_type, "value", new_type),
            duration_minutes=updates.get("duration_minutes", plan.duration_minutes),
            data_limit_mb=updates.get("data_limit_mb", plan.data_limit_mb),
            download_speed_kbps=updates.get("download_speed_kbps", plan.download_speed_kbps),
            upload_speed_kbps=updates.get("upload_speed_kbps", plan.upload_speed_kbps),
            price_ghs=updates.get("price_ghs", plan.price_ghs),
        )
        if duplicate:
            raise HTTPException(status_code=409, detail=f'{DUPLICATE_PLAN_MESSAGE} ("{duplicate.name}")')

    for field, value in updates.items():
        setattr(plan, field, value)

    await db.commit()
    await db.refresh(plan)
    return plan


@router.delete("/{plan_id}", status_code=204, responses={404: {"model": ErrorResponse}})
async def delete_plan(plan_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    result = await db.execute(select(Plan).where(Plan.id == plan_id, Plan.isp_operator_id == tenant.isp_operator_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    await db.delete(plan)
    await db.commit()
