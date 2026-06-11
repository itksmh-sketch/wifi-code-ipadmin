"""Admin + platform-owner WireGuard tunnel endpoints.

    POST   /api/v1/admin/routers/{router_id}/wireguard/setup
    GET    /api/v1/admin/routers/{router_id}/wireguard/status
    DELETE /api/v1/admin/routers/{router_id}/wireguard
    GET    /api/v1/admin/wireguard/peers            (platform owner only)
"""
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.base import get_db
from src.db.models import PlatformOwner, Router
from src.middleware.auth import TenantContext, get_admin_tenant_context, get_platform_owner_context, require_active_operator
from src.modules.platform.settings_service import get_setting
from src.modules.wireguard.keys import generate_wireguard_keypair
from src.modules.wireguard.service import WireGuardError, WireGuardService
from src.utils.encryption import decrypt_secret, encrypt_secret

logger = structlog.get_logger(__name__)
settings = get_settings()

router = APIRouter(prefix="/admin", tags=["wireguard"])

wg = WireGuardService()


def _iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def _load_router(db: AsyncSession, router_id: uuid.UUID, tenant: TenantContext) -> Router:
    result = await db.execute(
        select(Router).where(Router.id == router_id, Router.isp_operator_id == tenant.isp_operator_id)
    )
    router_row = result.scalar_one_or_none()
    if router_row is None:
        raise HTTPException(status_code=404, detail="Router not found")
    return router_row


@router.post("/routers/{router_id}/wireguard/setup")
async def setup_wireguard(
    router_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_active_operator),
):
    router_row = await _load_router(db, router_id, tenant)

    if not settings.wg_server_public_key:
        raise HTTPException(status_code=503, detail="WireGuard is not configured on the server (missing WG_SERVER_PUBLIC_KEY)")

    # Idempotent: reuse the existing keypair + IP if already set up, so we never
    # orphan a peer on the server by regenerating keys.
    if router_row.wg_peer_public_key and router_row.wg_peer_private_key_encrypted and router_row.wg_tunnel_ip:
        private_key = decrypt_secret(router_row.wg_peer_private_key_encrypted)
        public_key = router_row.wg_peer_public_key
        tunnel_ip = str(router_row.wg_tunnel_ip)
    else:
        private_key, public_key = generate_wireguard_keypair()
        tunnel_ip = await wg.allocate_tunnel_ip(db, str(router_row.id))
        router_row.wg_peer_private_key_encrypted = encrypt_secret(private_key)
        router_row.wg_peer_public_key = public_key
        router_row.wg_tunnel_ip = tunnel_ip
        router_row.wg_enabled = True
        await db.commit()
        await db.refresh(router_row)

    # Add (or re-assert) the peer on the running interface via the sidecar.
    try:
        await wg.add_peer(public_key, tunnel_ip)
    except WireGuardError as exc:
        logger.error("wg_setup_add_peer_failed", router_id=str(router_id), error=str(exc))
        raise HTTPException(status_code=502, detail=f"Tunnel record saved, but the VPN server could not add the peer: {exc}")

    endpoint = await get_setting(db, "wg_server_endpoint")
    commands = wg.build_mikrotik_commands(private_key, tunnel_ip, endpoint=endpoint)
    logger.info("wg_setup_complete", router_id=str(router_id), tunnel_ip=tunnel_ip)
    return {
        "tunnel_ip": tunnel_ip,
        "server_public_key": settings.wg_server_public_key,
        "server_endpoint": endpoint,
        "router_public_key": public_key,
        "mikrotik_commands": commands,
        "setup_complete": False,
    }


@router.get("/routers/{router_id}/wireguard/status")
async def wireguard_status(
    router_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    router_row = await _load_router(db, router_id, tenant)
    if not router_row.wg_enabled or not router_row.wg_peer_public_key:
        return {
            "enabled": False,
            "connected": False,
            "tunnel_ip": str(router_row.wg_tunnel_ip) if router_row.wg_tunnel_ip else None,
            "last_handshake_at": None,
            "transfer_rx": 0,
            "transfer_tx": 0,
        }

    # Try live status from the sidecar; fall back to last-known DB state if the
    # sidecar is unreachable (never 500 the dashboard).
    try:
        status = await wg.get_peer_status(router_row.wg_peer_public_key)
        connected = bool(status.get("connected"))
        last_handshake_at = _iso(status.get("last_handshake"))
        router_row.wg_is_connected = connected
        if status.get("last_handshake"):
            router_row.wg_last_handshake_at = datetime.fromtimestamp(status["last_handshake"], tz=timezone.utc)
        await db.commit()
        return {
            "enabled": True,
            "connected": connected,
            "tunnel_ip": str(router_row.wg_tunnel_ip) if router_row.wg_tunnel_ip else None,
            "last_handshake_at": last_handshake_at,
            "transfer_rx": status.get("transfer_rx", 0),
            "transfer_tx": status.get("transfer_tx", 0),
        }
    except WireGuardError as exc:
        logger.warning("wg_status_sidecar_unreachable", router_id=str(router_id), error=str(exc))
        return {
            "enabled": True,
            "connected": bool(router_row.wg_is_connected),
            "tunnel_ip": str(router_row.wg_tunnel_ip) if router_row.wg_tunnel_ip else None,
            "last_handshake_at": router_row.wg_last_handshake_at.isoformat() if router_row.wg_last_handshake_at else None,
            "transfer_rx": 0,
            "transfer_tx": 0,
            "stale": True,
        }


@router.delete("/routers/{router_id}/wireguard", status_code=200)
async def remove_wireguard(
    router_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    router_row = await _load_router(db, router_id, tenant)
    public_key = router_row.wg_peer_public_key

    # Remove the peer from the interface first (best-effort — don't block the
    # DB cleanup if the sidecar is down; the peer can be reaped later).
    if public_key:
        try:
            await wg.remove_peer(public_key)
        except WireGuardError as exc:
            logger.warning("wg_remove_peer_failed", router_id=str(router_id), error=str(exc))

    await wg.deallocate_tunnel_ip(db, str(router_row.id))
    router_row.wg_enabled = False
    router_row.wg_peer_public_key = None
    router_row.wg_peer_private_key_encrypted = None
    router_row.wg_tunnel_ip = None
    router_row.wg_is_connected = False
    router_row.wg_last_handshake_at = None
    await db.commit()
    logger.info("wg_tunnel_removed", router_id=str(router_id))
    return {"ok": True, "router_id": str(router_id)}


# --- platform owner: all peers across the fleet ---

peers_router = APIRouter(prefix="/admin/wireguard", tags=["wireguard"])


@peers_router.get("/peers")
async def list_all_peers(
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    # DB view of every WG-enabled router, keyed by public key.
    rows = (
        await db.execute(
            select(Router).where(Router.wg_enabled == True)  # noqa: E712
        )
    ).scalars().all()
    by_pubkey = {r.wg_peer_public_key: r for r in rows if r.wg_peer_public_key}

    # Live status from the sidecar (best-effort).
    live: dict[str, dict] = {}
    try:
        for peer in await wg.list_peers():
            live[peer["public_key"]] = peer
    except WireGuardError as exc:
        logger.warning("wg_peers_sidecar_unreachable", error=str(exc))

    result = []
    for pubkey, r in by_pubkey.items():
        peer = live.get(pubkey, {})
        result.append({
            "router_id": str(r.id),
            "router_name": r.name,
            "isp_operator_id": str(r.isp_operator_id),
            "public_key": pubkey,
            "tunnel_ip": str(r.wg_tunnel_ip) if r.wg_tunnel_ip else None,
            "connected": bool(peer.get("connected", r.wg_is_connected)),
            "last_handshake_at": _iso(peer.get("last_handshake")),
            "transfer_rx": peer.get("transfer_rx", 0),
            "transfer_tx": peer.get("transfer_tx", 0),
        })
    return result
