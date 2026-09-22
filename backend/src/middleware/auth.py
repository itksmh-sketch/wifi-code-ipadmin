from dataclasses import dataclass
import uuid

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone
from src.db.models import AdminUser, PlatformOwner
from src.db.base import get_db
from src.middleware.rate_limit import enforce_rate_limit
from src.utils.auth import decode_jwt_any_issuer, verify_platform_owner_token, verify_token

security = HTTPBearer()


@dataclass(frozen=True)
class TenantContext:
    is_platform_owner: bool
    isp_operator_id: uuid.UUID | None
    user_id: uuid.UUID
    role: str
    email: str


ONBOARDING_REQUIRED_HEADER = "X-Onboarding-Required"
# Set on the 403 from require_recent_pin. Value is the reason, so the caller
# knows which prompt to raise: "setup" (no PIN on the account yet), "verify"
# (elevation absent or expired) or "locked" (too many wrong entries).
PIN_REQUIRED_HEADER = "X-Pin-Required"


def token_version_matches(payload: dict, user: "AdminUser | PlatformOwner") -> bool:
    """A token is only valid for the account's current token_version (admin users
    and platform owners alike). Tokens minted before the claim existed carry none
    and are rejected (a one-time re-login)."""
    claim = payload.get("tv")
    return isinstance(claim, int) and not isinstance(claim, bool) and claim == int(user.token_version or 0)


async def get_authenticated_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> AdminUser:
    """The admin behind a valid, current access token — whether or not they have
    finished onboarding. Only the onboarding endpoints should depend on this
    directly; everything else uses get_current_user."""
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "admin:api", limit=120, window_seconds=60)

    payload = verify_token(credentials.credentials)
    if payload is None or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    user_id = payload.get("sub")
    isp_operator_id = payload.get("isp_operator_id")
    if not user_id or not isp_operator_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    result = await db.execute(
        select(AdminUser).where(
            AdminUser.id == user_id,
            AdminUser.isp_operator_id == isp_operator_id,
            AdminUser.is_active == True,
        )
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    if not token_version_matches(payload, user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session is no longer valid. Please sign in again.")

    return user


async def get_current_user(user: AdminUser = Depends(get_authenticated_admin)) -> AdminUser:
    """An authenticated admin with no pending account-setup step. Every admin API
    route goes through this (directly or via get_admin_tenant_context /
    require_role), so an account on a temp password can reach nothing but the
    onboarding endpoints — including after a platform-owner password reset."""
    if user.must_complete_onboarding:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Finish setting up your account: verify your phone and choose a new password.",
            headers={ONBOARDING_REQUIRED_HEADER: "1"},
        )
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your password was reset. Choose a new password to continue.",
            headers={ONBOARDING_REQUIRED_HEADER: "1"},
        )
    return user


def pin_elevation_error(user: AdminUser) -> HTTPException | None:
    """The 403 a gated route should raise, or None when the admin is elevated.

    Split out from require_recent_pin because one caller cannot express itself
    as a dependency: POST /auth/me/pin gates a PIN *change* but must stay open
    for a first-time *setup*, and a dependency runs before the handler knows
    which it is. Both paths share this one definition of "elevated".
    """
    now = datetime.now(timezone.utc)
    if not user.pin_hash:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Set a security PIN to use this area.",
            headers={PIN_REQUIRED_HEADER: "setup"},
        )
    if user.pin_locked_until is not None and user.pin_locked_until > now:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Too many incorrect PIN entries. Try again later.",
            headers={PIN_REQUIRED_HEADER: "locked"},
        )
    if user.pin_verified_until is None or user.pin_verified_until <= now:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Enter your PIN to continue.",
            headers={PIN_REQUIRED_HEADER: "verify"},
        )
    return None


async def require_recent_pin(user: AdminUser = Depends(get_current_user)) -> AdminUser:
    """An admin who entered their PIN within the elevation window.

    Layered on top of get_current_user, so everything that guard enforces still
    applies. Costs no extra query even when a route already depends on
    get_admin_tenant_context: that resolves through the same get_current_user,
    which FastAPI caches per request, and the elevation state is three columns
    on the row it already loaded.

    The three failure modes are distinguished by the X-Pin-Required header
    rather than by status code, so the frontend can tell "you have no PIN yet"
    from "enter it" from "you are locked out" and show the right thing. Unlike
    X-Onboarding-Required, this is not a redirect signal — the page stays put
    and raises a PIN prompt over itself.
    """
    error = pin_elevation_error(user)
    if error is not None:
        raise error
    return user


async def get_tenant_context(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> TenantContext:
    payload = decode_jwt_any_issuer(credentials.credentials)
    if payload is None or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    issuer = payload.get("iss")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    if issuer == "platform_owner":
        return TenantContext(
            is_platform_owner=True,
            isp_operator_id=None,
            user_id=uuid.UUID(str(user_id)),
            role="platform_owner",
            email=str(payload.get("email") or ""),
        )
    if issuer in ("admin", "reseller"):
        operator_id = payload.get("isp_operator_id")
        if not operator_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
        return TenantContext(
            is_platform_owner=False,
            isp_operator_id=uuid.UUID(str(operator_id)),
            user_id=uuid.UUID(str(user_id)),
            role=str(payload.get("role") or issuer),
            email=str(payload.get("email") or ""),
        )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token issuer")


async def get_admin_tenant_context(
    user: AdminUser = Depends(get_current_user),
) -> TenantContext:
    return TenantContext(
        is_platform_owner=False,
        isp_operator_id=user.isp_operator_id,
        user_id=user.id,
        role=user.role,
        email=user.email,
    )


async def get_platform_owner_context(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> PlatformOwner:
    payload = verify_platform_owner_token(credentials.credentials)
    if payload is None or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    owner_id = payload.get("sub")
    if not owner_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    result = await db.execute(select(PlatformOwner).where(PlatformOwner.id == owner_id, PlatformOwner.is_active == True))
    owner = result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Platform owner not found or inactive")
    if not token_version_matches(payload, owner):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session is no longer valid. Please sign in again.")
    return owner


def require_role(*roles: str):
    """Dependency factory to require specific admin roles."""
    async def _check(user: AdminUser = Depends(get_current_user)):
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user
    return _check


async def update_last_login(user_id, db: AsyncSession):
    await db.execute(
        update(AdminUser)
        .where(AdminUser.id == user_id)
        .values(last_login_at=datetime.now(timezone.utc))
    )
    await db.commit()


SUSPENDED_DETAIL = "Account suspended — pay your invoice to restore access"


async def assert_operator_not_suspended(db: AsyncSession, isp_operator_id) -> None:
    """Raise 403 if this operator is suspended. Shared by both guards below.

    A missing operator row is deliberately NOT a suspension — the same
    `if operator and ...` shape the captive portal and the reseller purchase
    path use. An absent row is a data problem, and failing closed on it would
    lock an operator out of their own account over one.

    What this guard does NOT cover, on purpose:

      * login (admin and reseller) — a suspended operator must be able to sign
        in, see they are suspended, and reach the billing page to pay;
      * POST /billing/invoices/{id}/pay and the whole PIN chain it depends on
        (require_recent_pin) — that is the only path out of suspension, and
        blocking any link in it is a permanent lockout;
      * the payment callback and both provider webhooks — a customer who has
        already paid must always be able to complete;
      * refunds, disconnects, revocations and deletions — everything that
        reduces access or returns money stays open.

    Suspension gates what creates sellable capacity, not what winds it down.
    """
    from src.db.models import ISPOperator
    operator = (
        await db.execute(select(ISPOperator).where(ISPOperator.id == isp_operator_id))
    ).scalar_one_or_none()
    if operator and operator.status == "suspended":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=SUSPENDED_DETAIL,
        )


async def require_active_operator(
    tenant: TenantContext = Depends(get_admin_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> TenantContext:
    """Blocks write operations when the operator is suspended."""
    await assert_operator_not_suspended(db, tenant.isp_operator_id)
    return tenant


# Marker read by tests/test_suspension_enforcement.py, which walks the live
# FastAPI route table and asserts the guard is present on exactly the endpoints
# policy says it should be — and, just as importantly, ABSENT from the
# exemption chain. A closure is otherwise hard to identify in a dependant tree.
require_active_operator.is_suspension_guard = True


def require_active_role(*roles: str):
    """require_role + the suspension gate, for routes that need both.

    Returns the AdminUser, exactly as require_role does — several routes
    annotate that as TenantContext and rely on `.isp_operator_id`, so the
    return type must not change.
    """
    async def _check(
        user: AdminUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> AdminUser:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        await assert_operator_not_suspended(db, user.isp_operator_id)
        return user
    _check.is_suspension_guard = True  # see require_active_operator below
    return _check
