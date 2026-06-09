from fastapi import APIRouter, Depends, HTTPException, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.base import get_db
from src.config import get_settings
from src.radius.accounting import (
    handle_accounting_start,
    handle_accounting_interim_update,
    handle_accounting_stop,
    handle_postauth,
)
import structlog

logger = structlog.get_logger(__name__)
settings = get_settings()

router = APIRouter(prefix="/radius", tags=["radius-internal"])


def verify_radius_secret(authorization: str | None = None):
    """Verify the RADIUS accounting shared secret."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization")
    # Accept both "Bearer <token>" and raw token
    token = authorization.replace("Bearer ", "").strip()
    if token != settings.radius_accounting_secret:
        raise HTTPException(status_code=403, detail="Invalid RADIUS secret")


@router.post("/accounting")
async def radius_accounting(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
):
    """
    Handle RADIUS Accounting packets from FreeRADIUS.
    Protected by shared secret (not JWT).
    """
    verify_radius_secret(authorization)

    body = await request.json()
    status_type = body.get("acct_status_type", "").lower()

    logger.info("radius_accounting_received", module=__name__, status_type=status_type)

    try:
        if status_type == "start":
            await handle_accounting_start(db, body)
        elif status_type == "interim-update":
            await handle_accounting_interim_update(db, body)
        elif status_type == "stop":
            await handle_accounting_stop(db, body)
        else:
            logger.warning("radius_accounting_unknown_status", module=__name__, status_type=status_type)

        return {"status": "ok"}
    except Exception as e:
        logger.error("radius_accounting_error", module=__name__, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/postauth")
async def radius_postauth(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
):
    """
    Handle RADIUS Post-Authentication logging.
    Protected by shared secret (not JWT).
    """
    verify_radius_secret(authorization)

    body = await request.json()
    try:
        await handle_postauth(db, body)
        return {"status": "ok"}
    except Exception as e:
        logger.error("radius_postauth_error", module=__name__, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
