"""End a cancelled operator's hotspot service when its wind-down runs out.

From isp_operators.hotspot_service_ends_at (migration 058) RADIUS refuses the
operator's logins on its own (freeradius/sql.conf authorize_check_query), and
during the wind-down it caps Session-Timeout at the time left. What remains
are clients still online with a session that began before the cancellation.
Once per wind-down (hotspot_service_cut_off_at, migration 059) this job:

  * sends a CoA disconnect for every open session row on the operator's own
    routers (radius.coa_events: the router's wg_tunnel_ip, User-Name +
    Framed-IP-Address, hotspot cookies cleared), then removes any active
    entry and cookie still on a reachable router through its API
    (access_revocation.revoke_voucher_access);
  * sets hotspot_service_cut_off_at and writes one system audit row,
    operator.hotspot_service_ended, with the counts. An operator with nobody
    online still gets the row.

After that: a failed CoA is retried by coa_retry (3 attempts, 2 minutes
apart), and reconcile_hotspot treats every voucher of an operator whose end
has passed as revoked, so every 5 minutes it removes them from any reachable
router, including clients that never got a session row. A router that is
offline is cleaned up when it comes back; its clients cannot log in again
meanwhile.

The operator row is not held locked while routers are contacted (a CoA waits
up to 3 s each). The mark is set at the end by a conditional UPDATE: if a
restore cleared the end date meanwhile, no row matches, nothing is marked or
audited, and the disconnects already sent stand (those clients can log in
again at once).
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import select, update

from src.db.base import async_session_factory
from src.db.models import ISPOperator, Router, Session, Voucher
from src.jobs.job_lock import job_lock
from src.modules import audit
from src.modules.mikrotik.access_revocation import revoke_voucher_access
from src.radius.coa_events import create_pending_disconnect_event, send_disconnect_with_event

logger = structlog.get_logger(__name__)

_LOCK_KEY = "lock:end_hotspot_service"
# A run sends one CoA (3 s timeout) and one router API call per open session;
# bounded so a crashed worker can't wedge the job.
_LOCK_TTL_SECONDS = 600


async def end_hotspot_service(ctx=None) -> dict:
    """Cron entry point (worker.py: every minute)."""
    async with job_lock((ctx or {}).get("redis"), _LOCK_KEY, _LOCK_TTL_SECONDS) as acquired:
        if not acquired:
            logger.info("hotspot_service_end_skipped_overlap")
            return {"skipped": "previous run still in flight"}
        return await end_due_operators()


async def end_due_operators(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stats = {"operators": 0, "sessions": 0, "coa_sent": 0, "coa_failed": 0, "errors": 0}
    async with async_session_factory() as db:
        due = (
            await db.execute(
                select(ISPOperator.id).where(
                    ISPOperator.hotspot_service_ends_at <= now,
                    ISPOperator.hotspot_service_cut_off_at.is_(None),
                )
            )
        ).scalars().all()
    for operator_id in due:
        # One transaction per operator: one operator's failure leaves the
        # others' cut-offs committed, and it is picked up again next minute.
        async with async_session_factory() as db:
            try:
                counts = await _end_one(db, operator_id, now)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                stats["errors"] += 1
                logger.error("hotspot_service_end_failed", isp_operator_id=str(operator_id), error=str(exc))
                continue
        if counts is not None:
            stats["operators"] += 1
            stats["sessions"] += counts["sessions"]
            stats["coa_sent"] += counts["coa_sent"]
            stats["coa_failed"] += counts["coa_failed"]
    if due:
        logger.info("hotspot_service_end_done", **stats)
    return stats


async def _end_one(db, operator_id, now: datetime) -> dict | None:
    operator = await db.get(ISPOperator, operator_id)
    if operator is None:
        return None
    rows = (
        await db.execute(
            select(Session, Router, Voucher)
            .join(Router, Router.id == Session.router_id)
            .join(Voucher, Voucher.id == Session.voucher_id)
            .where(
                Session.isp_operator_id == operator_id,
                Router.isp_operator_id == operator_id,
                Voucher.isp_operator_id == operator_id,
                Session.stopped_at.is_(None),
            )
            .order_by(Session.started_at)
        )
    ).all()

    counts = {"sessions": len(rows), "coa_sent": 0, "coa_failed": 0, "active_removed": 0,
              "cookies_cleared": 0, "router_errors": 0}
    for session, router, voucher in rows:
        event = await create_pending_disconnect_event(
            db, isp_operator_id=operator_id, voucher_id=voucher.id, router_id=router.id, session_row_id=session.id,
        )
        await send_disconnect_with_event(db, event=event, router=router, voucher=voucher, session=session)
        counts["coa_sent" if event.status == "sent" else "coa_failed"] += 1
    # Router-side cleanup after the CoAs (run first, it made every CoA NAK:
    # the session was already gone), as voucher_expiry does.
    for voucher in {v.id: v for _, _, v in rows}.values():
        result = await revoke_voucher_access(db, voucher)
        counts["active_removed"] += result["active_removed"]
        counts["cookies_cleared"] += result["cookies_cleared"]
        counts["router_errors"] += result["errors"]

    marked = await db.execute(
        update(ISPOperator)
        .where(
            ISPOperator.id == operator_id,
            ISPOperator.hotspot_service_ends_at <= now,
            ISPOperator.hotspot_service_cut_off_at.is_(None),
        )
        .values(hotspot_service_cut_off_at=now)
    )
    if marked.rowcount != 1:
        # Restored (or ended by another run) while the routers were contacted.
        logger.info("hotspot_service_end_not_marked", isp_operator_id=str(operator_id), **counts)
        return None
    audit.record_system(
        db,
        "operator.hotspot_service_ended",
        isp_operator_id=operator_id,
        target_type="isp_operator",
        target_id=operator_id,
        summary=f"Ended hotspot service for {operator.name}",
        detail={"hotspot_service_ends_at": operator.hotspot_service_ends_at.isoformat(), **counts},
    )
    logger.info("hotspot_service_ended", isp_operator_id=str(operator_id), **counts)
    return counts
