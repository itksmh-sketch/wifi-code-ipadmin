from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import delete, select

from src.db.base import async_session_factory
from src.db.models import Router, RouterCredential, RouterMetric
from src.modules.mikrotik.api_service import MikroTikAPIService

logger = structlog.get_logger(__name__)


async def collect_router_metrics(ctx=None) -> dict:
    service = MikroTikAPIService()
    async with async_session_factory() as db:
        result = await db.execute(
            select(Router.id)
            .join(RouterCredential, RouterCredential.router_id == Router.id)
            .where(RouterCredential.connection_status == "online")
        )
        router_ids = [str(router_id) for router_id in result.scalars().all()]

    outcomes = await asyncio.gather(*(collect_one_router(service, router_id) for router_id in router_ids), return_exceptions=True)
    collected = 0
    failed = 0
    for router_id, outcome in zip(router_ids, outcomes):
        if isinstance(outcome, Exception):
            failed += 1
            logger.error("router_metrics_collection_failed", router_id=router_id, error=str(outcome))
        else:
            collected += 1
    return {"routers_seen": len(router_ids), "collected": collected, "failed": failed}


async def collect_one_router(service: MikroTikAPIService, router_id: str) -> None:
    system_info, active_users = await asyncio.gather(
        service.get_system_info(router_id),
        service.get_active_hotspot_users(router_id),
    )
    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        metric = RouterMetric(
            router_id=router_id,
            collected_at=now,
            cpu_load_percent=system_info.cpu_load,
            memory_used_percent=_memory_used_percent(system_info.free_memory, system_info.total_memory),
            uptime_seconds=system_info.uptime_seconds,
            active_sessions=len(active_users),
            total_tx_bytes=sum(int(user.bytes_out or 0) for user in active_users),
            total_rx_bytes=sum(int(user.bytes_in or 0) for user in active_users),
            board_name=system_info.board_name,
            ros_version=system_info.ros_version,
        )
        db.add(metric)
        cutoff = now - timedelta(hours=24)
        await db.execute(delete(RouterMetric).where(RouterMetric.router_id == router_id, RouterMetric.collected_at < cutoff))
        await db.commit()


def _memory_used_percent(free_memory: int | None, total_memory: int | None) -> int | None:
    if free_memory is None or total_memory in (None, 0):
        return None
    used = total_memory - free_memory
    return int((used / total_memory) * 100)
