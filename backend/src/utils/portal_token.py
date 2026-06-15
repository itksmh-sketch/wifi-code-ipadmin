"""Signed tokens that bind a downloaded MikroTik login page to a specific router.

The token is baked into the router's ``login.html`` at provisioning time and
travels back to ``/portal/login`` on every client redirect, letting the portal
resolve the operator by ``router_id`` instead of fuzzy gateway-IP matching.

Uses a dedicated secret (``PORTAL_TOKEN_SECRET``) so that no other JWT in the
system — admin/operator sessions, platform-owner tokens, etc. — can ever be
accepted here even if one is passed in. Tokens have no expiry (Option A): the
binding is valid for the life of the router's provisioning.
"""
from __future__ import annotations

from jose import JWTError, jwt

from src.config import get_settings

_ALGORITHM = "HS256"
_PURPOSE = "portal_redirect"


def create_portal_router_token(router_id: str) -> str:
    """Mint a signed token binding a login page to ``router_id``."""
    payload = {"router_id": str(router_id), "purpose": _PURPOSE}
    return jwt.encode(payload, get_settings().portal_token_secret, algorithm=_ALGORITHM)


def decode_portal_router_token(token: str | None) -> str | None:
    """Return the ``router_id`` for a valid portal token, else ``None``.

    Rejects anything that isn't signed with the portal secret or is missing the
    ``portal_redirect`` purpose claim — so a stray session/admin JWT is ignored.
    Never raises; a bad token simply yields ``None`` so callers can fall back.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, get_settings().portal_token_secret, algorithms=[_ALGORITHM])
    except JWTError:
        return None
    if payload.get("purpose") != _PURPOSE:
        return None
    router_id = payload.get("router_id")
    return str(router_id) if router_id else None
