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


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> AdminUser:
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


async def require_active_operator(
    tenant: TenantContext = Depends(get_admin_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> TenantContext:
    """Blocks write operations when the operator is suspended."""
    from src.db.models import ISPOperator
    operator = (
        await db.execute(select(ISPOperator).where(ISPOperator.id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if operator and operator.status == "suspended":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account suspended — pay your invoice to restore access",
        )
    return tenant
