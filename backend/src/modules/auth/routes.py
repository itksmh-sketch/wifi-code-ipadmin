import logging
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.base import get_db
from src.db.models import AdminSecurityEvent, AdminUser
from src.middleware.rate_limit import enforce_rate_limit
from src.modules import audit
from src.modules.admin_accounts import lockout
from src.modules.admin_accounts.access import admin_may_sign_in
from src.schemas import LoginRequest, TokenResponse, RefreshRequest, ErrorResponse
from src.utils.auth import (
    hash_password,
    verify_token,
    verify_password,
)
from src.middleware.auth import token_version_matches, update_last_login
from src.modules.auth.tokens import admin_token_response
from sqlalchemy import func, select, update

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("auth.routes")

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


def _login_rejected(background_tasks: BackgroundTasks, user: AdminUser, reason: str) -> JSONResponse:
    """The 401 for a failed sign-in to a real account, with the attempt
    audited. Only accounts that exist are audited (an unknown email never
    reaches here), and the row is written as a background task, after the
    response has gone: an insert on this path but not on the unknown-email one
    would be a timing difference, which is the oracle the shared message and
    _DUMMY_HASH exist to remove.

    Returned, not raised, for the reason given at the lockout branch below:
    background tasks only run when attached to the response the handler
    returns. Same status and body as the raise.
    """
    background_tasks.add_task(
        audit.record_detached,
        "auth.login_failed",
        actor=audit.Actor.system(),
        isp_operator_id=user.isp_operator_id,
        target_type="admin_user",
        target_id=user.id,
        summary=f"Failed sign-in for {user.email}",
        detail={"reason": reason},
        request_info=audit.current_request_info(),
    )
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": INVALID_CREDENTIALS},
        background=background_tasks,
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={401: {"model": ErrorResponse}},
    dependencies=[Depends(audit.audited)],
)
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
        # admin_may_sign_in: a disabled admin, or one whose operator's logins
        # are disabled, is simply not found — the unknown-email path below,
        # dummy hash and all. No new branch, so nothing to tell them apart by.
        .where(func.lower(AdminUser.email) == email, admin_may_sign_in())
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
        return _login_rejected(background_tasks, user, "locked")

    if not verify_password(body.password, user.password_hash):
        event_id = await lockout.register_failure(db, user, lockout.LOGIN, client_ip=client_ip)
        if event_id is not None:
            # Backgrounded: the admin does not need the SMS to have landed
            # before this response, and an Arkesel round trip must not be on
            # the critical path of a login.
            background_tasks.add_task(lockout.send_lockout_notification, user.id, lockout.LOGIN, event_id)
        # RETURNED, not raised. FastAPI attaches background tasks to the
        # response the handler returns; when a handler raises, the exception
        # handler builds a fresh response and the tasks are silently dropped —
        # the lockout SMS and the audit row would never be written. The body
        # is byte-identical to the raise on the unknown-email path, so this
        # stays invisible to callers.
        return _login_rejected(background_tasks, user, "wrong_password")

    # A correct password ends the run of failures, lapsed lock and all.
    lockout.clear(user, lockout.LOGIN)
    # PIN elevation is stored on the account, not the token, so without this a
    # new sign-in inside someone else's 15-minute window would inherit it:
    # the password alone would open the PIN-gated areas. Every sign-in starts
    # unelevated. (Committed by update_last_login below, as lockout.clear is.)
    user.pin_verified_until = None
    # Committed with the last-login stamp by update_last_login.
    audit.record(db, "auth.login", actor=audit.Actor.for_admin(user), target_type="admin_user", target_id=user.id)
    await update_last_login(user.id, db)
    return admin_token_response(user)


@router.post("/refresh", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    payload = verify_token(body.refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    user_id = payload.get("sub")
    result = await db.execute(select(AdminUser).where(AdminUser.id == user_id, admin_may_sign_in()))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    if not token_version_matches(payload, user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    return admin_token_response(user)


class AdminLogoutRequest(BaseModel):
    refresh_token: Optional[str] = None


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(audit.audited)])
async def logout(
    request: Request,
    body: Optional[AdminLogoutRequest] = None,
    db: AsyncSession = Depends(get_db),
):
    """Sign out everywhere: bumping token_version ends every session this
    admin has, on every device, at once (it is checked on every request and at
    refresh). Only this admin's: the version is a column on their own row, so
    colleagues stay signed in. The PIN elevation is cleared with it, since it
    lives on the row too.

    Reads the token itself rather than depending on get_authenticated_admin /
    get_current_user, so no gate stands in front of it: it works mid-onboarding
    (temp password, unverified phone, no security question), for an operator
    that is suspended (it is in the suspension exemption chain), and for a
    live session of an account whose login is locked. Takes the access token
    (Authorization header) or a refresh token (body), as the platform-owner
    sign-out does.

    Always 204. A bad, expired or already-revoked token has no session left to
    end, and the dashboard signs out locally whatever the answer. The bump only
    applies while the token's version is still current, so a replayed sign-out
    is a no-op rather than a second bump."""
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "admin:logout", limit=20, window_seconds=60)

    candidates = []
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and credentials.strip():
        candidates.append((credentials.strip(), "access"))
    if body is not None and body.refresh_token:
        candidates.append((body.refresh_token, "refresh"))

    for token, kind in candidates:
        payload = verify_token(token)
        if payload is None or payload.get("type") != kind:
            continue
        version = payload.get("tv")
        if not isinstance(version, int) or isinstance(version, bool):
            continue
        try:
            admin_id = uuid.UUID(str(payload.get("sub")))
            operator_id = uuid.UUID(str(payload.get("isp_operator_id")))
        except ValueError:
            continue
        signed_out = (
            await db.execute(
                update(AdminUser)
                .where(
                    AdminUser.id == admin_id,
                    AdminUser.isp_operator_id == operator_id,
                    admin_may_sign_in(),
                    AdminUser.token_version == version,
                )
                .values(token_version=AdminUser.token_version + 1, pin_verified_until=None)
                .returning(AdminUser.id, AdminUser.email)
            )
        ).one_or_none()
        if signed_out is None:
            await db.rollback()
            continue
        db.add(AdminSecurityEvent(
            admin_user_id=admin_id,
            isp_operator_id=operator_id,
            event_type="signed_out",
            detail={"via": kind, "client_ip": client_ip},
        ))
        audit.record(
            db,
            "auth.logout",
            actor=audit.Actor(audit.OPERATOR_USER, admin_id, signed_out.email, operator_id),
            target_type="admin_user",
            target_id=admin_id,
            detail={"via": kind},
        )
        await db.commit()
        logger.info("admin_logout admin_id=%s via=%s", admin_id, kind)
        break
    return Response(status_code=status.HTTP_204_NO_CONTENT)
