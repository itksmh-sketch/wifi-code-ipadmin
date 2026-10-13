"""Job: audit_retention — nightly purge of audit rows past retention.

audit_log is append-only (migration 055): a trigger refuses every UPDATE and
TRUNCATE, and every DELETE unless the transaction has run
``SET LOCAL audit.purge = 'on'``. This job is the one place that does. The
window is LOCAL, so it closes with each transaction: one batch per
transaction, the setting, the delete and the job's own audit row together.
Batching keeps each transaction short (no long lock on the table the whole
app writes to) and PgBouncer's transaction pooling is fine with it, since the
setting never outlives the transaction it was made in.

Each batch writes one row (actor "system", platform-wide) saying how many rows
older than which instant it removed, so the trail records its own trimming.
A batch whose row can't be written rolls back with it: no deletion goes
unrecorded.
"""
from __future__ import annotations

from datetime import datetime, timezone

import calendar

import structlog
from sqlalchemy import text

from src.config import get_settings
from src.db.base import async_session_factory
from src.jobs.job_lock import job_lock
from src.modules import audit

logger = structlog.get_logger(__name__)

_LOCK_KEY = "lock:audit_retention"
_LOCK_TTL_SECONDS = 3600
# Bounds one night's work; anything left is picked up the next night.
_MAX_BATCHES = 200

_DELETE_BATCH = text(
    """
    DELETE FROM audit_log
    WHERE id IN (
        SELECT id FROM audit_log
        WHERE created_at < :cutoff
        ORDER BY created_at
        LIMIT :batch
    )
    """
)


def months_before(moment: datetime, months: int) -> datetime:
    """``moment`` minus whole calendar months, the day clamped to the target
    month's length (31 March minus one month is 28 or 29 February)."""
    index = moment.year * 12 + (moment.month - 1) - months
    year, month = divmod(index, 12)
    month += 1
    return moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1]))


def retention_cutoff(now: datetime | None = None) -> datetime:
    months = get_settings().audit_retention_months
    if months < 1:
        raise ValueError("audit_retention_months must be at least 1")
    return months_before(now or datetime.now(timezone.utc), months)


async def purge_batch(cutoff: datetime, batch_size: int) -> int:
    """Delete up to ``batch_size`` of the oldest rows before ``cutoff``, in one
    transaction with the purge window open and its own row. Returns the count."""
    async with async_session_factory() as db:
        async with db.begin():
            await db.execute(text("SET LOCAL audit.purge = 'on'"))
            deleted = (await db.execute(_DELETE_BATCH, {"cutoff": cutoff, "batch": batch_size})).rowcount or 0
            if deleted:
                audit.record_system(
                    db, "audit.purge", isp_operator_id=None, target_type="audit_log",
                    summary=f"Removed {deleted} audit rows older than {cutoff.date()}",
                    detail={"deleted": deleted, "cutoff": cutoff,
                            "retention_months": get_settings().audit_retention_months},
                )
        return deleted


async def purge_audit_log(ctx=None) -> dict:
    """Nightly. Purges in batches until nothing is past retention or the
    night's batch budget is spent."""
    async with job_lock((ctx or {}).get("redis"), _LOCK_KEY, _LOCK_TTL_SECONDS) as acquired:
        if not acquired:
            logger.info("audit_retention_skipped_overlap")
            return {"skipped": "previous run still in flight"}
        cutoff = retention_cutoff()
        batch_size = max(1, get_settings().audit_purge_batch_size)
        total, batches = 0, 0
        while batches < _MAX_BATCHES:
            deleted = await purge_batch(cutoff, batch_size)
            batches += 1
            total += deleted
            if deleted < batch_size:
                break
        logger.info("audit_retention_completed", deleted=total, batches=batches, cutoff=cutoff.isoformat())
        return {"deleted": total, "batches": batches, "cutoff": cutoff.isoformat()}
