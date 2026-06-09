from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from src.db.base import get_db
from src.db.models import Session
from src.schemas import SessionResponse, SessionListResponse, ErrorResponse
from src.middleware.auth import TenantContext, get_admin_tenant_context
from typing import Optional
import uuid

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    voucher_id: Optional[uuid.UUID] = Query(None),
    router_id: Optional[uuid.UUID] = Query(None),
    active_only: bool = Query(False),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    query = select(Session).where(Session.isp_operator_id == tenant.isp_operator_id)
    count_query = select(func.count()).select_from(Session).where(Session.isp_operator_id == tenant.isp_operator_id)

    if voucher_id:
        query = query.where(Session.voucher_id == voucher_id)
        count_query = count_query.where(Session.voucher_id == voucher_id)
    if router_id:
        query = query.where(Session.router_id == router_id)
        count_query = count_query.where(Session.router_id == router_id)
    if active_only:
        query = query.where(Session.stopped_at.is_(None))
        count_query = count_query.where(Session.stopped_at.is_(None))

    query = query.order_by(Session.started_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    sessions = result.scalars().all()

    count_result = await db.execute(count_query)
    total = count_result.scalar()

    return SessionListResponse(sessions=sessions, total=total)


@router.get("/{session_id}", response_model=SessionResponse, responses={404: {"model": ErrorResponse}})
async def get_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    result = await db.execute(select(Session).where(Session.id == session_id, Session.isp_operator_id == tenant.isp_operator_id))
    session = result.scalar_one_or_none()
    if not session:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Session not found")
    return session
