"""Persistence helpers for the per-router setup wizard status.

Backs the ``router_setup_status`` table (migration 015) so the UI can show each
section's state even when the router is offline. ``detected_at`` is kept inside
the per-section ``*_config`` JSONB (under ``_detected_at``); the dedicated
``*_applied_at`` columns track the last successful apply.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import RouterSetupStatus

SECTIONS = ("network", "hotspot", "radius", "nat")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def get_or_create(db: AsyncSession, router_id) -> RouterSetupStatus:
    row = (
        await db.execute(select(RouterSetupStatus).where(RouterSetupStatus.router_id == router_id))
    ).scalar_one_or_none()
    if row is None:
        row = RouterSetupStatus(router_id=router_id)
        db.add(row)
        await db.flush()
    return row


async def record_detect(db: AsyncSession, router_id, section: str, status: str, config: dict[str, Any] | None = None) -> RouterSetupStatus:
    row = await get_or_create(db, router_id)
    # Merge into (not replace) the existing config so values persisted by a prior
    # apply — e.g. network's hotspot_network, which the NAT section depends on —
    # survive a later re-detect. Detect payloads live under "detected", so they
    # never collide with the flat keys written on apply.
    stored = dict(getattr(row, f"{section}_config") or {})
    stored.update(config or {})
    stored["_detected_at"] = _utcnow().isoformat()
    setattr(row, f"{section}_status", status)
    setattr(row, f"{section}_config", stored)
    await db.commit()
    await db.refresh(row)
    return row


async def record_apply(db: AsyncSession, router_id, section: str, status: str, config: dict[str, Any] | None = None) -> RouterSetupStatus:
    row = await get_or_create(db, router_id)
    setattr(row, f"{section}_status", status)
    if status != "error":
        setattr(row, f"{section}_applied_at", _utcnow())
    if config is not None:
        existing = dict(getattr(row, f"{section}_config") or {})
        existing.update(config)
        setattr(row, f"{section}_config", existing)
    await db.commit()
    await db.refresh(row)
    return row


def section_payload(row: RouterSetupStatus | None, section: str) -> dict[str, Any]:
    if row is None:
        return {"status": "unconfigured", "detected_at": None, "last_applied_at": None, "config": None}
    config = dict(getattr(row, f"{section}_config") or {})
    detected_at = config.pop("_detected_at", None)
    applied_at = getattr(row, f"{section}_applied_at")
    return {
        "status": getattr(row, f"{section}_status") or "unconfigured",
        "detected_at": detected_at,
        "last_applied_at": applied_at.isoformat() if applied_at else None,
        "config": config or None,
    }


def sections_complete(row: RouterSetupStatus | None) -> int:
    if row is None:
        return 0
    return sum(1 for s in SECTIONS if getattr(row, f"{s}_status") == "configured")
