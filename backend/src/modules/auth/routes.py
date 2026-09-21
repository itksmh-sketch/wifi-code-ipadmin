from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.base import get_db
from src.db.models import AdminUser, PlatformOwner
from src.middleware.rate_limit import enforce_rate_limit
from src.modules.admin_accounts import lockout
from src.schemas import LoginRequest, TokenResponse, RefreshRequest, ErrorResponse
from src.utils.auth import (
    hash_password,
    verify_platform_owner_token,
    verify_token,
    verify_password,
)
from src.middleware.auth import token_version_matches, update_last_login
from src.modules.auth.tokens import admin_token_response, platform_owner_token_response
from sqlalchemy import func, select

router = APIRouter(prefix="/auth", tags=["auth"])

# Every failure mode of admin login answers with exactly this — wrong password,
# unknown email, inactive account, and locked-out alike. A distinct "your
# account is locked" reply would turn the endpoint into an oracle for which
# addresses are real and which are under attack. The admin learns about a lock
# from the SMS sent to their verified phone, which is a channel an attacker who
# only has the email address cannot read.
INVALID_CREDENTIALS = "Invalid email or password"

# Verified against when there is nothing real to verify against, so that the
# no-such-account and locked-out paths cost about the same as a genuine bcrypt
# check. Without this, the early returns would be measurably faster than a
# wrong password and leak exactly what the shared message is hiding.
_DUMMY_HASH = hash_password("dummy-password-for-timing-equalisation")


@router.post("/login", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
async def login(
    body: LoginRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "admin:login", limit=10, window_seconds=60)

    email = (body.email or "").strip().lower()
    result = await db.execute(
        select(AdminUser)
        .where(func.lower(AdminUser.email) == email, AdminUser.is_active == True)
        # Row-locked for the duration: two concurrent wrong guesses must count
        # as two, not one. Contention is per-account, so a busy login endpoint
        # serialises only attempts against the same email.
        .with_for_update()
    )
    user = result.scalar_one_or_none()

    if user is None:
        verify_password(body.password, _DUMMY_HASH)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS)

    if lockout.is_locked(user, lockout.LOGIN):
        # Rejected before the counter can move, which is also what keeps the
        # lockout SMS to one per lock: no increment, so no further threshold
        # crossing, so no repeat send while this lock holds.
        verify_password(body.password, _DUMMY_HASH)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS)

    if not verify_password(body.password, user.password_hash):
        event_id = await lockout.register_failure(db, user, lockout.LOGIN, client_ip=client_ip)
        if event_id is not None:
            # Backgrounded: the admin does not need the SMS to have landed
            # before this response, and an Arkesel round trip must not be on
            # the critical path of a login.
            background_tasks.add_task(lockout.send_lockout_notification, user.id, lockout.LOGIN, event_id)
            # RETURNED, not raised. FastAPI attaches background tasks to the
            # response the handler returns; when a handler raises, the
            # exception handler builds a fresh response and the tasks are
            # silently dropped — the lockout SMS would never be sent. The body
            # is byte-identical to the raise below, so this stays invisible to
            # callers.
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": INVALID_CREDENTIALS},
                background=background_tasks,
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS)

    # A correct password ends the run of failures, lapsed lock and all.
    lockout.clear(user, lockout.LOGIN)
    await update_last_login(user.id, db)
    return admin_token_response(user)


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
    if not token_version_matches(payload, user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    return admin_token_response(user)


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

    return platform_owner_token_response(owner)


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
    if not token_version_matches(payload, owner):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    return platform_owner_token_response(owner)
