"""Background job: refresh WireGuard tunnel connection status for all routers.

Runs every 2 minutes (see worker.py). For each WG-enabled router it asks the
wg-manager sidecar for the peer's handshake/transfer state and updates
``wg_is_connected`` / ``wg_last_handshake_at``. A just-online router is also
marked reachable. Never fails the whole run if the sidecar is unreachable —
it logs and continues (per constraint #6).
"""
from datetime import datetime, timezone

import structlog
from sqlalchemy import select

from src.db.base import async_session_factory
from src.db.models import Router
from src.modules.wireguard.service import WireGuardError, WireGuardService

logger = structlog.get_logger(__name__)


async def check_wireguard_tunnels(ctx=None) -> dict:
    stats = {"checked": 0, "online": 0, "offline": 0, "errors": 0, "transitions": 0}
    wg = WireGuardService()

    async with async_session_factory() as db:
        routers = (
            await db.execute(
                select(Router).where(
                    Router.wg_enabled == True,  # noqa: E712
                    Router.wg_peer_public_key.isnot(None),
                )
            )
        ).scalars().all()

        for router in routers:
            stats["checked"] += 1
            try:
                status = await wg.get_peer_status(router.wg_peer_public_key)
            except WireGuardError as exc:
                stats["errors"] += 1
                logger.warning("wg_status_check_failed", router_id=str(router.id), error=str(exc))
                continue

            was_connected = bool(router.wg_is_connected)
            connected = bool(status.get("connected"))
            router.wg_is_connected = connected
            last_handshake = status.get("last_handshake")
            if last_handshake:
                router.wg_last_handshake_at = datetime.fromtimestamp(last_handshake, tz=timezone.utc)

            if connected:
                stats["online"] += 1
                # Tunnel is up -> the router is reachable.
                router.is_online = True
                router.last_seen_at = datetime.now(timezone.utc)
            else:
                stats["offline"] += 1

            if connected != was_connected:
                stats["transitions"] += 1
                logger.info(
                    "wg_tunnel_transition",
                    router_id=str(router.id),
                    site_id=str(router.site_id),
                    connected=connected,
                )

        await db.commit()

    logger.info("wg_tunnel_check_done", **stats)
    return stats
