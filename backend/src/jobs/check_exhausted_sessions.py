"""Backstop job: close exhausted/expired sessions that CoA failed to disconnect.

Gap this fills
--------------
expire_vouchers marks a voucher exhausted and fires one CoA disconnect.
coa_retry retries up to 3 times (2-minute spacing), then abandons.  After that,
a client whose CoA attempts all failed (flaky tunnel, router briefly unreachable,
CoA listener misconfigured) stays online consuming uncapped data until
Session-Timeout fires — potentially days for a data-only plan.

Detection
---------
Primary condition: voucher.status IN ('exhausted','expired') AND
session.stopped_at IS NULL.  This is the source of truth; coa_events is used
only as an anti-overlap guard (exclude sessions where a CoA is still pending or
within coa_retry's 2-minute retry window).

Action
------
RouterOS API removal (/ip/hotspot/active remove) via MikroTikAPIService — a
genuinely different mechanism from CoA (TCP 8728 vs UDP 3799, same path as the
manual-disconnect button which proved more reliable).  Falls back to a direct
CoA send when no API credentials are stored for the router (weak: re-fires the
mechanism that already failed, but better than nothing).

RouterOS active-list cross-check also cleans up phantom sessions: if the client
is NOT in the active list, Accounting-Stop was lost — we write stopped_at +
terminate_cause='lost-acct-stop' and the session exits future detection cleanly.

Retry policy
------------
Runs every 5 minutes.  Gated on router reachability (wg_is_connected OR
is_online — same as setup_routes._is_online).  Skips offline routers silently;
retries automatically when the router comes back online.  Indefinite retry until
the session is actually closed (stopped_at set) or the router is confirmed to
have the client in its active list and removal succeeds.
"""
from datetime import datetime, timedelta, timezone
from itertools import groupby

import structlog
from sqlalchemy import and_, exists, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import async_session_factory
from src.db.models import CoAEvent, Router, RouterCredential, Session, Voucher
from src.modules.mikrotik.api_service import MikroTikAPIService
from src.radius.coa_sender import send_disconnect_request
from src.utils.encryption import decrypt_secret

logger = structlog.get_logger(__name__)


async def check_exhausted_sessions(ctx=None) -> dict:
    stats = {
        "candidates": 0,
        "api_disconnected": 0,
        "phantoms_closed": 0,
        "coa_fallback_sent": 0,
        "coa_fallback_failed": 0,
        "skipped_offline": 0,
        "api_errors": 0,
    }

    now = datetime.now(timezone.utc)
    # Mirrors coa_retry's exact 2-minute cooldown window so the backstop never
    # fires on a session that coa_retry is actively working.
    coa_retry_cutoff = now - timedelta(minutes=2)

    async with async_session_factory() as db:
        # Correlated subquery: TRUE when a CoA disconnect for this voucher is
        # pending (just fired by expire_vouchers) or failed but still within
        # coa_retry's active window (attempt_count < 3, retried < 2 min ago).
        coa_inflight = (
            select(CoAEvent.id)
            .where(
                CoAEvent.voucher_id == Voucher.id,
                CoAEvent.event_type == "disconnect",
                or_(
                    CoAEvent.status == "pending",
                    and_(
                        CoAEvent.status == "failed",
                        CoAEvent.attempt_count < 3,
                        CoAEvent.last_attempted_at > coa_retry_cutoff,
                    ),
                ),
            )
            .correlate(Voucher)
            .exists()
        )

        rows = (
            await db.execute(
                select(Session, Voucher, Router)
                .join(Voucher, Voucher.id == Session.voucher_id)
                .join(Router, Router.id == Session.router_id)
                .where(
                    Voucher.status.in_(["exhausted", "expired"]),
                    Session.stopped_at.is_(None),
                    not_(coa_inflight),
                )
                .order_by(Session.router_id)
            )
        ).all()

        if not rows:
            logger.debug("backstop_no_candidates")
            return stats

        service = MikroTikAPIService()

        # Group by router so we make one API connection per router, not per session.
        sorted_rows = sorted(rows, key=lambda r: str(r[2].id))
        for router_id_str, group in groupby(sorted_rows, key=lambda r: str(r[2].id)):
            group_list = list(group)
            router: Router = group_list[0][2]

            # Gate on reachability — mirrors setup_routes._is_online() logic.
            if not (bool(router.is_online) or bool(router.wg_enabled and router.wg_is_connected)):
                stats["skipped_offline"] += len(group_list)
                logger.debug(
                    "backstop_router_offline_skip",
                    router_id=router_id_str,
                    skipped=len(group_list),
                )
                continue

            cred = (
                await db.execute(
                    select(RouterCredential).where(RouterCredential.router_id == router.id)
                )
            ).scalar_one_or_none()

            if cred is not None:
                await _handle_via_api(db, service, router_id_str, group_list, stats)
            else:
                await _handle_via_coa_fallback(router, group_list, stats)

        await db.commit()

    logger.info("backstop_check_done", **stats)
    return stats


async def _handle_via_api(
    db: AsyncSession,
    service: MikroTikAPIService,
    router_id_str: str,
    entries: list,
    stats: dict,
) -> None:
    """Fetch the router's active hotspot list once, then cross-check every
    candidate session.  One API connection per router regardless of session count.
    ActiveUserInfo.id is already resolved via _routeros_id() inside api_service —
    no .get('.id') here.
    """
    try:
        active_users = await service.get_active_hotspot_users(router_id_str)
    except Exception as exc:
        stats["api_errors"] += len(entries)
        logger.warning(
            "backstop_active_list_failed",
            router_id=router_id_str,
            count=len(entries),
            error=str(exc),
        )
        return

    active_by_username = {u.user: u for u in active_users if u.user}

    for session, voucher, _ in entries:
        stats["candidates"] += 1

        if session.username not in active_by_username:
            # Client absent from router's active list: Accounting-Stop was lost.
            # Close the stale session row so it drops out of future detection.
            await db.execute(
                update(Session)
                .where(Session.id == session.id)
                .values(
                    stopped_at=datetime.now(timezone.utc),
                    terminate_cause="lost-acct-stop",
                )
            )
            stats["phantoms_closed"] += 1
            logger.info(
                "backstop_phantom_closed",
                session_id=str(session.id),
                username=session.username,
                voucher_id=str(voucher.id),
                router_id=router_id_str,
            )
        else:
            active_entry = active_by_username[session.username]
            try:
                await service.disconnect_hotspot_user(router_id_str, active_entry.id)
                await db.execute(
                    update(Session)
                    .where(Session.id == session.id)
                    .values(
                        stopped_at=datetime.now(timezone.utc),
                        terminate_cause="backstop-api-disconnect",
                    )
                )
                stats["api_disconnected"] += 1
                logger.info(
                    "backstop_api_disconnected",
                    session_id=str(session.id),
                    username=session.username,
                    voucher_id=str(voucher.id),
                    router_id=router_id_str,
                    routeros_active_id=active_entry.id,
                )
            except Exception as exc:
                stats["api_errors"] += 1
                logger.warning(
                    "backstop_api_remove_failed",
                    session_id=str(session.id),
                    username=session.username,
                    router_id=router_id_str,
                    error=str(exc),
                )


async def _handle_via_coa_fallback(
    router: Router,
    entries: list,
    stats: dict,
) -> None:
    """CoA fallback when the router has no stored API credentials.

    Re-fires a direct Disconnect-Request (bypassing coa_events so we don't
    restart the retry cycle).  Weak: re-uses the mechanism that already failed
    3×.  May succeed if the router's CoA listener recovered since coa_retry
    gave up.  Does NOT close the session row — if CoA succeeds the router sends
    Accounting-Stop which closes it; if CoA fails again the backstop retries
    on the next 5-minute tick.
    """
    target_ip = (
        str(router.wg_tunnel_ip)
        if router.wg_tunnel_ip
        else (str(router.ip_address) if router.ip_address else None)
    )
    if not target_ip:
        return

    try:
        nas_secret = decrypt_secret(router.nas_secret)
    except Exception as exc:
        logger.warning(
            "backstop_coa_fallback_decrypt_failed",
            router_id=str(router.id),
            error=str(exc),
        )
        return

    for session, voucher, _ in entries:
        stats["candidates"] += 1
        if not session.ip_address:
            continue

        result = send_disconnect_request(
            router_ip=target_ip,
            router_secret=nas_secret,
            attributes={
                "User-Name": session.username,
                "Framed-IP-Address": str(session.ip_address),
            },
        )
        if result.get("status") == "success":
            stats["coa_fallback_sent"] += 1
            logger.info(
                "backstop_coa_fallback_sent",
                session_id=str(session.id),
                username=session.username,
                voucher_id=str(voucher.id),
                router_id=str(router.id),
            )
        else:
            stats["coa_fallback_failed"] += 1
            logger.warning(
                "backstop_coa_fallback_failed",
                session_id=str(session.id),
                username=session.username,
                router_id=str(router.id),
                error=result.get("message"),
            )
