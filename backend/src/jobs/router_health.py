from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import structlog
from src.db.base import async_session_factory
from src.db.models import Router

logger = structlog.get_logger(__name__)


async def check_router_health(ctx=None) -> dict:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=10)
    stats = {"offline_marked": 0}

    async with async_session_factory() as db:
        routers = (
            await db.execute(
                select(Router).where(
                    Router.is_active == True,  # noqa: E712
                )
            )
        ).scalars().all()

        for router in routers:
            is_stale = router.last_seen_at is None or router.last_seen_at < cutoff
            if not is_stale:
                continue
            if router.is_online:
                logger.warning(
                    "router_went_offline",
                    module=__name__,
                    router_id=str(router.id),
                    site_id=str(router.site_id),
                    last_seen_at=str(router.last_seen_at) if router.last_seen_at else None,
                )
            router.is_online = False
            stats["offline_marked"] += 1

        await db.commit()

    return stats
