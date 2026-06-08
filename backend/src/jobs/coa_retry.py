from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.db.base import async_session_factory
from src.db.models import CoAEvent, Router, Session, Voucher
from src.radius.coa_events import send_disconnect_with_event
import structlog

logger = structlog.get_logger(__name__)


async def retry_failed_coa_events(ctx=None) -> dict:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=2)
    stats = {"retried": 0, "still_failed": 0, "sent": 0}

    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(CoAEvent).where(
                    CoAEvent.status == "failed",
                    CoAEvent.attempt_count < 3,
                    CoAEvent.last_attempted_at.is_not(None),
                    CoAEvent.last_attempted_at < cutoff,
                )
            )
        ).scalars().all()

        for event in rows:
            stats["retried"] += 1
            router = await db.get(Router, event.router_id) if event.router_id else None
            voucher = await db.get(Voucher, event.voucher_id) if event.voucher_id else None
            session = await db.get(Session, event.session_id) if event.session_id else None

            if not router or not voucher:
                event.status = "failed"
                event.attempt_count = int(event.attempt_count or 0) + 1
                event.last_attempted_at = now
                event.error_message = "missing_router_or_voucher"
            else:
                await send_disconnect_with_event(
                    db,
                    event=event,
                    router=router,
                    voucher=voucher,
                    session=session,
                )

            if event.status == "sent":
                stats["sent"] += 1
            else:
                stats["still_failed"] += 1
                if int(event.attempt_count or 0) >= 3:
                    logger.error(
                        "coa_retry_exhausted",
                        module=__name__,
                        router_id=str(event.router_id) if event.router_id else None,
                        voucher_id=str(event.voucher_id) if event.voucher_id else None,
                        attempt_count=int(event.attempt_count or 0),
                    )

        await db.commit()

    return stats
