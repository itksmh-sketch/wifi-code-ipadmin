"""Resolve the RADIUS server address to configure on / verify against a router.

Phase 1 made the platform VPN-only: the backend reaches every router over its
WireGuard tunnel IP (see ``api_service._load_router_access``). The RADIUS client
we provision on the router must therefore point at the platform's *tunnel*
address (``WG_SERVER_TUNNEL_IP``, e.g. 10.100.0.1) — not ``RADIUS_PUBLIC_HOST``.
RADIUS_PUBLIC_HOST is only reachable over the router's old direct/public path,
which no longer exists for tunneled routers; pushing it produces the
``connect: Network unreachable`` status seen on the router's /radius entry.

``RADIUS_PUBLIC_HOST`` is kept solely as a fallback for a hypothetical
non-tunneled router. In practice that branch is unreachable for every
provisioning/diagnostic path: ``_load_router_access`` raises before the RADIUS
step can run if a router has no tunnel, so any router that reaches here has a
``wg_tunnel_ip``.
"""
from __future__ import annotations

from typing import Any, Optional

from src.config import Settings, get_settings


def resolve_radius_host(router: Any, settings: Optional[Settings] = None) -> str:
    """Return the RADIUS server address the given router should use.

    ``router`` may be an ORM ``Router`` or a ``RouterAccess`` — anything exposing
    a ``wg_tunnel_ip`` attribute. When a tunnel IP is present we always use the
    server's tunnel IP; otherwise we fall back to ``RADIUS_PUBLIC_HOST``.
    """
    if settings is None:
        settings = get_settings()
    tunnel_ip = getattr(router, "wg_tunnel_ip", None) if router is not None else None
    if tunnel_ip:
        return (settings.wg_server_tunnel_ip or "").strip()
    return (settings.radius_public_host or "").strip()
