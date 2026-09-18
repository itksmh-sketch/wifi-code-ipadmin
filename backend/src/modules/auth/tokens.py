"""Token payloads for the two admin-side issuers, in one place.

Both carry a ``tv`` (token_version) claim read from the account row: raising
that column invalidates every access and refresh token already issued for the
account. Login, refresh, and any endpoint that changes a password (which must
hand back a working pair so the caller isn't logged out of its own request)
build tokens through here, so no caller can forget the claim.
"""
from __future__ import annotations

from src.db.models import AdminUser, PlatformOwner
from src.schemas import TokenResponse
from src.utils.auth import (
    create_access_token,
    create_platform_owner_access_token,
    create_platform_owner_refresh_token,
    create_refresh_token,
)


def admin_token_data(user: AdminUser) -> dict:
    return {
        "sub": str(user.id),
        "role": user.role,
        "email": user.email,
        "isp_operator_id": str(user.isp_operator_id),
        # Current token_version: bumping it on the row invalidates this token.
        "tv": int(user.token_version or 0),
    }


def admin_token_response(user: AdminUser) -> TokenResponse:
    token_data = admin_token_data(user)
    return TokenResponse(
        access_token=create_access_token(token_data),
        refresh_token=create_refresh_token(token_data),
        must_complete_onboarding=bool(user.must_complete_onboarding),
        must_change_password=bool(user.must_change_password),
    )


def platform_owner_token_data(owner: PlatformOwner) -> dict:
    return {
        "sub": str(owner.id),
        "role": "platform_owner",
        "email": owner.email,
        "tv": int(owner.token_version or 0),
    }


def platform_owner_token_response(owner: PlatformOwner) -> TokenResponse:
    token_data = platform_owner_token_data(owner)
    return TokenResponse(
        access_token=create_platform_owner_access_token(token_data),
        refresh_token=create_platform_owner_refresh_token(token_data),
    )
