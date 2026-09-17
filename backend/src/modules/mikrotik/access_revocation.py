"""Router-side access revocation shared by every path that ends a client's access.

Why this exists
---------------
A CoA Disconnect (or /ip/hotspot/active remove) only ends the *current* hotspot
session. The router's stored HTTP/MAC login cookie survives it and can log the
client straight back in, so every access-ending path must also clear cookies.
Paths that rely on an open ``sessions`` row can also miss a client entirely when
that row was never written (e.g. an Acct-Session-Id collision), so voucher-level
revocation removes active entries by username rather than by session row.

Everything here is best-effort and never raises: it runs alongside the CoA
pipeline (coa_events / coa_retry / backstop), which remains the source of truth
for disconnect status. Routers that are offline or have no stored API
credentials are skipped — only the short cookie lifetime covers those.
"""
from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Router, RouterCredential, Session, Voucher
from src.modules.mikrotik.api_service import MikroTikAPIService

logger = structlog.get_logger(__name__)


def router_is_reachable(router: Router) -> bool:
    # Mirrors setup_routes._is_online / the backstop's gate.
    return bool(router.is_online) or bool(router.wg_enabled and router.wg_is_connected)


def voucher_login_names(voucher: Voucher | None, session: Session | None = None) -> list[str]:
    """Every name the router may know this client by (portal logins can use the
    voucher code or its username)."""
    names = []
    for name in (
        session.username if session else None,
        voucher.username if voucher else None,
        voucher.code if voucher else None,
    ):
        if name and name not in names:
            names.append(name)
    return names


async def _api_capable(db: AsyncSession, router: Router) -> bool:
    if not router.is_active or not router_is_reachable(router):
        return False
    cred = (
        await db.execute(select(RouterCredential.id).where(RouterCredential.router_id == router.id))
    ).scalar_one_or_none()
    return cred is not None


async def clear_router_cookies(
    db: AsyncSession,
    router: Router,
    usernames: list[str],
    mac: str | None = None,
    *,
    service: MikroTikAPIService | None = None,
) -> int | None:
    """Clear hotspot cookies for these names/MAC on one router. Returns the count,
    or None when the router was skipped or the call failed."""
    if not usernames and not mac:
        return None
    if not await _api_capable(db, router):
        return None
    try:
        cleared = await (service or MikroTikAPIService()).clear_hotspot_cookies(str(router.id), usernames, mac)
    except Exception as exc:
        logger.warning("hotspot_cookie_clear_failed", router_id=str(router.id), error=str(exc))
        return None
    if cleared:
        logger.info("hotspot_cookies_cleared", router_id=str(router.id), usernames=usernames, cleared=cleared)
    return cleared


async def revoke_voucher_access(
    db: AsyncSession,
    voucher: Voucher,
    *,
    service: MikroTikAPIService | None = None,
) -> dict[str, int]:
    """Remove every active hotspot entry and cookie for this voucher on every
    reachable router it has been used on — independent of whether an open
    session row exists."""
    stats = {"routers": 0, "active_removed": 0, "cookies_cleared": 0, "errors": 0}
    names = voucher_login_names(voucher)
    if not names:
        return stats

    routers = (
        await db.execute(
            select(Router)
            .where(
                Router.isp_operator_id == voucher.isp_operator_id,
                Router.id.in_(select(Session.router_id).where(Session.voucher_id == voucher.id).distinct()),
            )
        )
    ).scalars().all()

    service = service or MikroTikAPIService()
    for router in routers:
        if not await _api_capable(db, router):
            continue
        stats["routers"] += 1
        try:
            result = await service.remove_hotspot_user_access(str(router.id), names)
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("voucher_access_revoke_failed", router_id=str(router.id), voucher_id=str(voucher.id), error=str(exc))
            continue
        stats["active_removed"] += result.get("active_removed", 0)
        stats["cookies_cleared"] += result.get("cookies_cleared", 0)

    if stats["active_removed"] or stats["cookies_cleared"] or stats["errors"]:
        logger.info("voucher_access_revoked", voucher_id=str(voucher.id), **stats)
    return stats
