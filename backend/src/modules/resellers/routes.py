import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import Reseller
from src.middleware.rate_limit import enforce_rate_limit
from src.middleware.reseller_auth import get_current_reseller, reseller_token_current, update_reseller_last_login
from src.modules.admin_accounts.access import reseller_may_sign_in
from src.modules.resellers import consignment
from src.schemas import ErrorResponse, LoginRequest, RefreshRequest, TokenResponse
from src.utils.reseller_auth import (
    create_reseller_access_token,
    create_reseller_refresh_token,
    hash_reseller_password,
    verify_reseller_password,
    verify_reseller_token,
)

router = APIRouter(prefix="/reseller", tags=["reseller"])

# Unknown email, wrong password and a disabled account (or one whose operator's
# logins are off) all answer with exactly this, as admin login does.
INVALID_CREDENTIALS = "Invalid email or password"

# Verified against when no account matched, so that path costs about the same
# as a wrong password. Without it the unknown and disabled cases return before
# any bcrypt work and are measurably faster, which tells apart exactly what the
# shared message hides.
_DUMMY_HASH = hash_reseller_password("dummy-password-for-timing-equalisation")


def _tokens(reseller: Reseller) -> TokenResponse:
    token_data = {
        "sub": str(reseller.id),
        "role": reseller.role,
        "email": reseller.email,
        "isp_operator_id": str(reseller.isp_operator_id),
        "town_id": str(reseller.town_id) if reseller.town_id else None,
        "site_id": str(reseller.site_id) if reseller.site_id else None,
        # Checked on every request: bumping the reseller's token_version
        # (disable, password change) ends every session at once.
        "tv": int(reseller.token_version or 0),
    }
    return TokenResponse(
        access_token=create_reseller_access_token(token_data),
        refresh_token=create_reseller_refresh_token(token_data),
    )


@router.post("/auth/login", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
async def reseller_login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "reseller:login", limit=10, window_seconds=60)

    # Stored lowercased (admin_routes lowercases on create and edit), so a
    # reseller who types their address with capitals is still found.
    email = (body.email or "").strip().lower()
    result = await db.execute(
        select(Reseller).where(func.lower(Reseller.email) == email, reseller_may_sign_in())
    )
    reseller = result.scalar_one_or_none()
    if reseller is None:
        # Disabled resellers land here too: reseller_may_sign_in filters them out.
        verify_reseller_password(body.password, _DUMMY_HASH)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS)
    if not verify_reseller_password(body.password, reseller.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS)

    await update_reseller_last_login(reseller.id, db)

    return _tokens(reseller)


@router.post("/auth/refresh", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
async def reseller_refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    payload = verify_reseller_token(body.refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    reseller_id = payload.get("sub")
    result = await db.execute(
        select(Reseller).where(
            Reseller.id == reseller_id,
            Reseller.isp_operator_id == payload.get("isp_operator_id"),
            reseller_may_sign_in(),
        )
    )  # noqa: E712
    reseller = result.scalar_one_or_none()
    if not reseller or not reseller_token_current(payload, reseller):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Reseller not found or inactive")

    return _tokens(reseller)


@router.get("/me")
async def reseller_me(reseller: Reseller = Depends(get_current_reseller)):
    return {
        "id": str(reseller.id),
        "name": reseller.name,
        "email": reseller.email,
        "phone": reseller.phone,
        "role": reseller.role,
        "town_id": str(reseller.town_id) if reseller.town_id else None,
        "site_id": str(reseller.site_id) if reseller.site_id else None,
        "is_active": bool(reseller.is_active),
    }


# Read-only views of the reseller's own codes and account. Every query is
# scoped to the signed-in reseller and its operator; there is no reseller id in
# any path, so there is nothing to point at another reseller's rows.


@router.get("/summary")
async def reseller_summary(reseller: Reseller = Depends(get_current_reseller), db: AsyncSession = Depends(get_db)):
    return await consignment.summary(db, reseller.isp_operator_id, reseller.id)


@router.get("/codes")
async def reseller_codes(
    status: str | None = None, allocation_id: uuid.UUID | None = None, limit: int = 500,
    reseller: Reseller = Depends(get_current_reseller), db: AsyncSession = Depends(get_db),
):
    return await consignment.list_codes(db, reseller.isp_operator_id, reseller.id, state=status,
                                        allocation_id=allocation_id, limit=limit)


@router.get("/account")
async def reseller_account(reseller: Reseller = Depends(get_current_reseller), db: AsyncSession = Depends(get_db)):
    summary = await consignment.summary(db, reseller.isp_operator_id, reseller.id)
    # Who at the operator recorded each entry is the operator's business.
    ledger = await consignment.account(db, reseller.isp_operator_id, reseller.id, include_recorder=False)
    return {"totals": summary["totals"], **ledger}
