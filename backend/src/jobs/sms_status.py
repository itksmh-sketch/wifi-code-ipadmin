"""Jobs: refresh_sms_status (one message, enqueued by Arkesel's delivery
callback) and poll_sms_statuses (cron, every 5 minutes). Both read the status
from Arkesel's API; see src/modules/sms/status_tracking.py."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import structlog

from src.db.base import async_session_factory
from src.db.models import SMSMessage
from src.jobs.queue import get_redis_pool
from src.modules.sms import status_tracking

logger = structlog.get_logger(__name__)

REFRESH_JOB = "refresh_sms_status"


async def enqueue_status_refresh(message_id: uuid.UUID, reported_status: str | None) -> bool:
    """One job per (message, reported status): a repeated callback is a no-op
    while the job is kept, a later status (SUBMITTED -> DELIVERED) is not."""
    tag = re.sub(r"[^A-Za-z0-9_]", "", reported_status or "")[:24] or "none"
    redis = None
    try:
        redis = await get_redis_pool()
        await redis.enqueue_job(REFRESH_JOB, str(message_id), _job_id=f"sms_status:{message_id}:{tag}")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("sms_status_enqueue_failed", message_id=str(message_id), error=str(exc))
        return False
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:  # noqa: BLE001
                pass


async def refresh_sms_status(ctx, message_id: str) -> str:
    async with async_session_factory() as db:
        row = await db.get(SMSMessage, uuid.UUID(message_id))
        if row is None:
            return "missing"
        return await status_tracking.refresh_status(db, row, source="callback")


async def poll_sms_statuses(ctx=None) -> dict:
    now = datetime.now(timezone.utc)
    stats: dict[str, int] = {}
    async with async_session_factory() as db:
        rows = await status_tracking.pending_for_poll(db, now=now)
        for row in rows:
            try:
                outcome = await status_tracking.refresh_status(db, row, source="poll")
            except Exception as exc:  # noqa: BLE001 — one bad row must not stop the rest
                await db.rollback()
                logger.error("sms_status_poll_row_failed", message_id=str(row.id), error=str(exc))
                outcome = "error"
            stats[outcome] = stats.get(outcome, 0) + 1
    stats["checked"] = len(rows)
    return stats
