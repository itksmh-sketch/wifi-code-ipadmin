from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import async_session_factory
from src.db.models import Plan, Router, Session, Voucher
from src.jobs.job_lock import job_lock
from src.modules.vouchers.engine import transition_voucher_status
from src.modules.mikrotik.access_revocation import revoke_voucher_access
from src.radius.coa_events import create_pending_disconnect_event, send_disconnect_with_event
import structlog

logger = structlog.get_logger(__name__)

_LOCK_KEY = "lock:expire_vouchers"
# Comfortably above any real runtime (max 3.30s across 22,494 recorded runs),
# but bounded so a crashed worker self-heals instead of wedging the job.
_LOCK_TTL_SECONDS = 120


async def expire_vouchers(ctx=None):
    """
    Background job: runs every 60 seconds (worker.py registers it as
    cron(expire_vouchers, second=0)).
    Transitions active vouchers to expired or exhausted when limits are reached.
    Also sends CoA/Disconnect to terminate live sessions.

    Note on cap overshoot: this job can only act on data_used_mb, which is
    refreshed by the RADIUS interim-update (Acct-Interim-Interval = 60s, set in
    freeradius/sql.conf authorize_reply_query). So a voucher can exceed its cap
    by up to one interim interval plus up to one cron period before it is
    detected -- roughly 120s worst case, 60s typical.
    """
    # Guard against overlapping runs. At second={0,15,30,45} a run that stalls
    # could still be in flight when the next tick fires — most plausibly when a
    # batch of vouchers each need a CoA (3s socket timeout) plus a RouterOS API
    # call, all done inline and sequentially below. Two overlapping runs would
    # both see the same voucher as 'active' and each fire a disconnect.
    async with job_lock((ctx or {}).get("redis"), _LOCK_KEY, _LOCK_TTL_SECONDS) as acquired:
        if not acquired:
            logger.info("voucher_expiry_skipped_overlap", module=__name__)
            return {"skipped": "previous run still in flight"}
        await _expire_vouchers_locked()


async def _expire_vouchers_locked():
    now = datetime.now(timezone.utc)
    logger.info("voucher_expiry_started", module=__name__, now=str(now))

    async with async_session_factory() as db:
        try:
            expired_result = await db.execute(
                select(Voucher)
                .where(
                    Voucher.status == "active",
                    Voucher.expires_at.isnot(None),
                    Voucher.expires_at < now,
                )
            )
            expired_vouchers = expired_result.scalars().all()

            exhausted_result = await db.execute(
                select(Voucher)
                .join(Plan, Plan.id == Voucher.plan_id)
                .where(
                    Voucher.status == "active",
                    Plan.data_limit_mb.isnot(None),
                    Voucher.data_used_mb >= Plan.data_limit_mb,
                )
            )
            exhausted_vouchers = exhausted_result.scalars().all()

            expired_count = 0
            exhausted_count = 0

            for voucher in expired_vouchers:
                try:
                    await _transition_and_disconnect(db, voucher, "expired")
                    expired_count += 1
                    logger.info(
                        "voucher_expired",
                        module=__name__,
                        voucher_id=str(voucher.id),
                        code=voucher.code,
                    )
                except ValueError as e:
                    logger.warning(
                        "voucher_expiry_transition_failed",
                        module=__name__,
                        voucher_id=str(voucher.id),
                        code=voucher.code,
                        error=str(e),
                    )

            expired_ids = {voucher.id for voucher in expired_vouchers}
            for voucher in exhausted_vouchers:
                if voucher.id in expired_ids:
                    continue
                try:
                    await _transition_and_disconnect(db, voucher, "exhausted")
                    exhausted_count += 1
                    logger.info(
                        "voucher_exhausted_by_cron",
                        module=__name__,
                        voucher_id=str(voucher.id),
                        code=voucher.code,
                    )
                except ValueError as e:
                    logger.warning(
                        "voucher_exhaust_transition_failed",
                        module=__name__,
                        voucher_id=str(voucher.id),
                        code=voucher.code,
                        error=str(e),
                    )

            await db.commit()
            logger.info(
                "voucher_expiry_completed",
                module=__name__,
                expired_count=expired_count,
                exhausted_count=exhausted_count,
            )

        except Exception as e:
            logger.error(
                "voucher_expiry_failed",
                module=__name__,
                error=str(e),
            )
            await db.rollback()


async def _transition_and_disconnect(db: AsyncSession, voucher: Voucher, next_status: str):
    await transition_voucher_status(db, voucher.id, next_status, voucher.isp_operator_id)
    await _disconnect_open_session(db, voucher)
    # Router-side cleanup AFTER the CoA (running it first made every CoA NAK
    # because the session was already gone): removes any active entry the CoA
    # didn't reach — including a client with no session row at all — and
    # clears stored hotspot cookies. See access_revocation.
    await revoke_voucher_access(db, voucher)


async def _disconnect_open_session(db: AsyncSession, voucher: Voucher):
    session_result = await db.execute(
        select(Session)
        .where(
            Session.voucher_id == voucher.id,
            Session.isp_operator_id == voucher.isp_operator_id,
            Session.stopped_at.is_(None),
        )
        .order_by(Session.started_at.desc())
        .limit(1)
    )
    active_session = session_result.scalar_one_or_none()
    if not active_session:
        return

    router_result = await db.execute(select(Router).where(Router.id == active_session.router_id, Router.isp_operator_id == voucher.isp_operator_id))
    router = router_result.scalar_one_or_none()
    if not router:
        return

    refreshed_voucher = (
        await db.execute(select(Voucher).where(Voucher.id == voucher.id, Voucher.isp_operator_id == voucher.isp_operator_id))
    ).scalar_one_or_none()
    event = await create_pending_disconnect_event(
        db,
        isp_operator_id=voucher.isp_operator_id,
        voucher_id=voucher.id,
        router_id=router.id,
        session_row_id=active_session.id,
    )
    await send_disconnect_with_event(
        db,
        event=event,
        router=router,
        voucher=refreshed_voucher,
        session=active_session,
    )
    logger.info(
        "coa_disconnect_attempt",
        module=__name__,
        voucher_id=str(voucher.id),
        router_id=str(router.id),
        status=event.status,
        attempt_count=int(event.attempt_count or 0),
    )
