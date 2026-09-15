from __future__ import annotations
import uuid
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import OperatorApplication, PlatformOwner
from src.middleware.auth import get_platform_owner_context
from src.middleware.rate_limit import enforce_rate_limit
from src.modules.applications import service
from src.modules.applications.schemas import (
    ApplicationSubmit,
    ApplicationResponse,
    ApplicationReject,
    EmailCheckRequest,
    EmailCheckResponse,
)

public_router = APIRouter(prefix="/public", tags=["public"])
platform_router = APIRouter(prefix="/platform", tags=["platform"])

# Email availability pre-check: generous for a human filling in the form (the
# page debounces and caches), but caps how fast one IP can probe addresses.
EMAIL_CHECK_RATE_LIMIT = 20
EMAIL_CHECK_RATE_WINDOW_SECONDS = 60


@public_router.post("/apply/check-email", response_model=EmailCheckResponse)
async def check_application_email(
    body: EmailCheckRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Live availability hint for the public apply form.

    Unauthenticated, so it is an enumeration surface by nature. Two mitigations:
    the answer is a bare boolean (never admin vs. pending vs. approved), and it is
    rate-limited per IP against bulk harvesting. Submit re-checks authoritatively.
    """
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(
        client_ip,
        "public:apply-email-check",
        limit=EMAIL_CHECK_RATE_LIMIT,
        window_seconds=EMAIL_CHECK_RATE_WINDOW_SECONDS,
    )
    return {"available": not await service.email_in_use(db, body.email)}


@public_router.post("/apply", status_code=201)
async def submit_application(
    body: ApplicationSubmit,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "public:apply", limit=3, window_seconds=3600)

    try:
        app = await service.submit_application(db, body)
    except service.EmailUnavailableError:
        raise HTTPException(409, service.EMAIL_UNAVAILABLE_MESSAGE)
    return {
        "message": "Your application has been received. We'll review it and contact you within 24-48 hours.",
        "id": str(app.id),
    }


@platform_router.get("/applications", response_model=List[ApplicationResponse])
async def list_applications(
    status: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    q = select(OperatorApplication).order_by(OperatorApplication.created_at.desc())
    if status:
        q = q.where(OperatorApplication.status == status)
    if date_from:
        q = q.where(OperatorApplication.created_at >= date_from)
    if date_to:
        q = q.where(OperatorApplication.created_at <= date_to)
    result = await db.execute(q)
    return result.scalars().all()


@platform_router.get("/applications/{application_id}", response_model=ApplicationResponse)
async def get_application(
    application_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    app = (await db.execute(select(OperatorApplication).where(OperatorApplication.id == application_id))).scalar_one_or_none()
    if not app:
        raise HTTPException(404, "Application not found")
    return app


@platform_router.put("/applications/{application_id}/approve", status_code=200)
async def approve_application(
    application_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    # No request body: the new operator's monthly fee is stamped from the
    # platform default inside service.approve_application, never client-supplied.
    app = (await db.execute(select(OperatorApplication).where(OperatorApplication.id == application_id))).scalar_one_or_none()
    if not app:
        raise HTTPException(404, "Application not found")
    if app.status != "pending":
        raise HTTPException(400, f"Application is already {app.status}")
    # admin_users.email is UNIQUE: without this, approval dies on the INSERT with a 500.
    # Platform-owner-only endpoint, so the specific reason is fine here.
    if await service.admin_email_exists(db, app.email):
        raise HTTPException(
            409,
            "An admin account with this email already exists, so this application can't be approved. "
            "Reject it, or change the existing admin's email first.",
        )

    operator, temp_password = await service.approve_application(db, app, owner.id)
    return {
        "operator_id": str(operator.id),
        "slug": operator.slug,
        "admin_email": app.email,
        "temp_password": temp_password,
        "trial_ends_at": operator.trial_ends_at.isoformat(),
        "message": "Operator approved. Share the temp_password with the operator securely — it is shown only once.",
    }


@platform_router.put("/applications/{application_id}/reject", status_code=200)
async def reject_application(
    application_id: uuid.UUID,
    body: ApplicationReject,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    app = (await db.execute(select(OperatorApplication).where(OperatorApplication.id == application_id))).scalar_one_or_none()
    if not app:
        raise HTTPException(404, "Application not found")
    if app.status != "pending":
        raise HTTPException(400, f"Application is already {app.status}")

    app = await service.reject_application(db, app, owner.id, body.rejection_reason)
    return {"message": "Application rejected.", "id": str(app.id)}
