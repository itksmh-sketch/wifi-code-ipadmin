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

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from src.config import get_settings

_ALGORITHM = "HS256"
_PURPOSE = "portal_redirect"
_PREVIEW_PURPOSE = "portal_preview"
_PREVIEW_TTL = timedelta(hours=1)


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


def create_portal_preview_token(operator_id: str) -> str:
    """Mint a signed, short-lived token for the branding-settings mobile preview.

    Deliberately separate from create_portal_router_token: encodes operator_id
    (not router_id), carries no branding field values — those live in the
    Redis draft store, keyed by operator_id, not in this token — and expires
    on its own (unlike router tokens, which must survive for the router's
    provisioning lifetime). Re-minted each time the settings page loads."""
    payload = {
        "operator_id": str(operator_id),
        "purpose": _PREVIEW_PURPOSE,
        "exp": datetime.now(timezone.utc) + _PREVIEW_TTL,
    }
    return jwt.encode(payload, get_settings().portal_token_secret, algorithm=_ALGORITHM)


def decode_portal_preview_token(token: str | None) -> str | None:
    """Return the ``operator_id`` for a valid preview token, else ``None``.
    Same never-raises contract as decode_portal_router_token."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, get_settings().portal_token_secret, algorithms=[_ALGORITHM])
    except JWTError:
        return None
    if payload.get("purpose") != _PREVIEW_PURPOSE:
        return None
    operator_id = payload.get("operator_id")
    return str(operator_id) if operator_id else None
