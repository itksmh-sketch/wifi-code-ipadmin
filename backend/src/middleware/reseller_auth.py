from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import Reseller
from src.utils.reseller_auth import verify_reseller_token

security = HTTPBearer()


async def get_current_reseller(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> Reseller:
    payload = verify_reseller_token(credentials.credentials)
    if payload is None or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    reseller_id = payload.get("sub")
    isp_operator_id = payload.get("isp_operator_id")
    if not reseller_id or not isp_operator_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    result = await db.execute(
        select(Reseller).where(
            Reseller.id == reseller_id,
            Reseller.isp_operator_id == isp_operator_id,
            Reseller.is_active == True,
        )
    )  # noqa: E712
    reseller = result.scalar_one_or_none()
    if not reseller:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Reseller not found or inactive")
    return reseller


async def update_reseller_last_login(reseller_id, db: AsyncSession):
    await db.execute(
        update(Reseller)
        .where(Reseller.id == reseller_id)
        .values(last_login_at=datetime.now(timezone.utc))
    )
    await db.commit()
