from fastapi import APIRouter, Depends, HTTPException, Request, status
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.base import get_db
from src.db.models import AdminUser, PlatformOwner
from src.middleware.rate_limit import enforce_rate_limit
from src.schemas import LoginRequest, TokenResponse, RefreshRequest, ErrorResponse
from src.utils.auth import (
    create_access_token,
    create_platform_owner_access_token,
    create_platform_owner_refresh_token,
    create_refresh_token,
    verify_platform_owner_token,
    verify_token,
    verify_password,
)
from src.middleware.auth import update_last_login
from sqlalchemy import select

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "admin:login", limit=10, window_seconds=60)

    result = await db.execute(select(AdminUser).where(AdminUser.email == body.email, AdminUser.is_active == True))
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    await update_last_login(user.id, db)

    token_data = {"sub": str(user.id), "role": user.role, "email": user.email, "isp_operator_id": str(user.isp_operator_id)}
    access_token = create_access_token(token_data)
    refresh_token = create_refresh_token(token_data)

    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    payload = verify_token(body.refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    user_id = payload.get("sub")
    result = await db.execute(select(AdminUser).where(AdminUser.id == user_id, AdminUser.is_active == True))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    token_data = {"sub": str(user.id), "role": user.role, "email": user.email, "isp_operator_id": str(user.isp_operator_id)}
    return TokenResponse(
        access_token=create_access_token(token_data),
        refresh_token=create_refresh_token(token_data),
    )


@router.post("/platform/login", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
async def platform_login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "platform:login", limit=10, window_seconds=60)

    result = await db.execute(select(PlatformOwner).where(PlatformOwner.email == body.email, PlatformOwner.is_active == True))
    owner = result.scalar_one_or_none()
    if not owner or not verify_password(body.password, owner.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    owner.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    token_data = {"sub": str(owner.id), "role": "platform_owner", "email": owner.email}
    return TokenResponse(
        access_token=create_platform_owner_access_token(token_data),
        refresh_token=create_platform_owner_refresh_token(token_data),
    )


@router.post("/platform/refresh", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
async def platform_refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    payload = verify_platform_owner_token(body.refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    owner_id = payload.get("sub")
    result = await db.execute(select(PlatformOwner).where(PlatformOwner.id == owner_id, PlatformOwner.is_active == True))
    owner = result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Platform owner not found or inactive")

    token_data = {"sub": str(owner.id), "role": "platform_owner", "email": owner.email}
    return TokenResponse(
        access_token=create_platform_owner_access_token(token_data),
        refresh_token=create_platform_owner_refresh_token(token_data),
    )
