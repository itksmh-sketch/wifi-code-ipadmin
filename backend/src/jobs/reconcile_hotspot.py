"""Reconcile what each router says is online against what the backend believes.

Drift this catches
------------------
Every other disconnect path is driven by the DB (an open ``sessions`` row, a
voucher status change). When the DB is wrong they can't act at all — e.g. an
Acct-Session-Id collision meant a client's reconnect never got a sessions row,
so it was unmetered and "disable voucher" had nothing to disconnect. This job
starts from the router instead:

* ``revoked_online``   — router has an active entry for a voucher that is not
                         valid (disabled/expired/exhausted).
                         Remediated: active entry removed + cookies cleared.
* ``revoked_cookie``   — router holds a login cookie for a voucher that is not
                         valid. Remediated: cookie cleared.
* ``unknown_online``   — active entry whose name matches no voucher of the
                         router's operator (e.g. a local hotspot user). Flagged
                         only.
* ``untracked_online`` — valid voucher online on the router with no open
                         sessions row for it (the original bug state).
                         Flagged; kicked only when KICK_UNTRACKED is set.
* ``phantom_session``  — open sessions row whose client is not on the router.
                         Remediated: row closed (terminate_cause
                         'reconcile-not-on-router').

It also re-enforces the short hotspot cookie lifetime on every router it
reaches, so routers that were offline when the policy changed pick it up as
soon as they're back.

Only routers that are reachable and have stored API credentials are checked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import async_session_factory
from src.db.models import Router, RouterCredential, RouterProvisionLog, Session, Voucher
from src.modules.mikrotik.access_revocation import router_is_reachable
from src.modules.mikrotik.api_service import MikroTikAPIService

logger = structlog.get_logger(__name__)

# Grace periods: Accounting-Start lands a moment after the router lists the
# client, and Accounting-Stop a moment after it drops it.
UNTRACKED_GRACE = timedelta(minutes=2)
PHANTOM_GRACE = timedelta(minutes=5)

# Kicking a valid-but-untracked client forces a fresh login (and so a fresh
# Accounting-Start). Off by default: flag first, decide after seeing volume.
KICK_UNTRACKED = False

VALID_VOUCHER_STATUSES = {"unused", "active"}

_UPTIME_PART = re.compile(r"(\d+)([wdhms])")
_UPTIME_SECONDS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_uptime(value: str | None) -> timedelta | None:
    """RouterOS durations: '1w2d3h4m5s', '45s', or clock form '01:02:03'."""
    if not value:
        return None
    if ":" in value:
        parts = [int(p) for p in value.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return timedelta(hours=parts[0], minutes=parts[1], seconds=parts[2])
    matches = _UPTIME_PART.findall(value)
    if not matches:
        return None
    return timedelta(seconds=sum(int(n) * _UPTIME_SECONDS[u] for n, u in matches))


def voucher_is_valid(voucher: Any, now: datetime) -> bool:
    if voucher is None or voucher.status not in VALID_VOUCHER_STATUSES:
        return False
    return voucher.expires_at is None or voucher.expires_at > now


@dataclass
class ReconcilePlan:
    revoked_online: list[dict] = field(default_factory=list)
    revoked_cookie: list[dict] = field(default_factory=list)
    untracked_online: list[dict] = field(default_factory=list)
    unknown_online: list[dict] = field(default_factory=list)
    phantom_session: list[Any] = field(default_factory=list)


def plan_reconciliation(
    *,
    active: list[dict],
    cookies: list[dict],
    vouchers_by_name: dict[str, Any],
    open_sessions: list[Any],
    now: datetime,
) -> ReconcilePlan:
    """Pure classification — no I/O. ``active`` rows are ActiveUserInfo dumps,
    ``cookies`` rows come from get_hotspot_cookies()."""
    plan = ReconcilePlan()
    open_names = {s.username for s in open_sessions}
    active_names = {a.get("user") for a in active if a.get("user")}

    for entry in active:
        name = entry.get("user")
        if not name:
            continue
        if name not in vouchers_by_name:
            plan.unknown_online.append(entry)
            continue
        voucher = vouchers_by_name[name]
        if not voucher_is_valid(voucher, now):
            plan.revoked_online.append(entry)
        elif name not in open_names:
            uptime = parse_uptime(entry.get("uptime"))
            if uptime is None or uptime >= UNTRACKED_GRACE:
                plan.untracked_online.append(entry)

    for cookie in cookies:
        name = cookie.get("user")
        # Cookies for names we don't know (e.g. a local hotspot user) are left alone.
        if name in vouchers_by_name and not voucher_is_valid(vouchers_by_name[name], now):
            plan.revoked_cookie.append(cookie)

    for session in open_sessions:
        if session.username not in active_names and session.started_at < now - PHANTOM_GRACE:
            plan.phantom_session.append(session)

    return plan


COOKIE_POLICY_TRIGGER = "reconcile_hotspot:cookie_policy"


async def _enforce_cookie_policy(db: AsyncSession, service: MikroTikAPIService, router: Router, stats: dict) -> None:
    """Push the short cookie lifetime if the router drifted from it. Any actual
    change (or failure) is recorded durably in router_provision_log — visible on
    the router's provision-log list — not just logged, because this writes live
    config on routers nobody may be watching. A compliant router records nothing."""
    router_id = str(router.id)
    started = datetime.now(timezone.utc)
    try:
        changed = await service.enforce_cookie_policy(router_id)
    except Exception as exc:
        db.add(RouterProvisionLog(
            router_id=router.id, triggered_by=COOKIE_POLICY_TRIGGER, action="update_hotspot", status="failed",
            started_at=started, completed_at=datetime.now(timezone.utc), error_message=str(exc)[:500],
            commands_executed=[],
        ))
        await db.commit()
        logger.warning("reconcile_cookie_policy_failed", router_id=router_id, router_name=router.name, error=str(exc))
        raise
    if not changed:
        return
    db.add(RouterProvisionLog(
        router_id=router.id, triggered_by=COOKIE_POLICY_TRIGGER, action="update_hotspot", status="success",
        started_at=started, completed_at=datetime.now(timezone.utc),
        commands_executed=[{"change": c} for c in changed],
    ))
    await db.commit()
    stats["cookie_policy_applied"] += 1
    logger.warning("reconcile_cookie_policy_applied", router_id=router_id, router_name=router.name, changes=changed)


async def _reconcile_router(
    db: AsyncSession,
    service: MikroTikAPIService,
    router: Router,
    now: datetime,
    stats: dict,
    dry_run: bool,
    voucher_overrides: dict[str, str] | None,
) -> ReconcilePlan:
    router_id = str(router.id)
    if not dry_run:
        await _enforce_cookie_policy(db, service, router, stats)

    active = [u.model_dump() for u in await service.get_active_hotspot_users(router_id)]
    cookies = await service.get_hotspot_cookies(router_id)

    names = {r.get("user") for r in active + cookies if r.get("user")}
    vouchers_by_name: dict[str, Any] = {}
    if names:
        rows = (
            await db.execute(
                select(Voucher).where(
                    Voucher.isp_operator_id == router.isp_operator_id,
                    or_(Voucher.code.in_(names), Voucher.username.in_(names)),
                )
            )
        ).scalars().all()
        for v in rows:
            for n in (v.code, v.username):
                if n in names:
                    vouchers_by_name[n] = v
    if voucher_overrides:
        # Test hook: pretend a voucher has a different status (never persisted).
        for n, status in voucher_overrides.items():
            v = vouchers_by_name.get(n)
            if v is not None:
                vouchers_by_name[n] = SimpleNamespace(status=status, expires_at=v.expires_at, id=v.id)

    open_sessions = (
        await db.execute(select(Session).where(Session.router_id == router.id, Session.stopped_at.is_(None)))
    ).scalars().all()

    plan = plan_reconciliation(
        active=active, cookies=cookies, vouchers_by_name=vouchers_by_name, open_sessions=open_sessions, now=now
    )

    for entry in plan.untracked_online:
        stats["untracked_online"] += 1
        logger.warning(
            "reconcile_untracked_online",
            router_id=router_id,
            username=entry.get("user"),
            address=entry.get("address"),
            mac_address=entry.get("mac_address"),
            uptime=entry.get("uptime"),
            bytes_in=entry.get("bytes_in"),
            bytes_out=entry.get("bytes_out"),
            kicked=KICK_UNTRACKED and not dry_run,
        )

    for entry in plan.unknown_online:
        stats["unknown_online"] += 1
        logger.warning("reconcile_unknown_online", router_id=router_id, username=entry.get("user"), address=entry.get("address"))

    to_revoke = {e.get("user") for e in plan.revoked_online}
    if KICK_UNTRACKED:
        to_revoke |= {e.get("user") for e in plan.untracked_online}
    cookie_only = {c.get("user") for c in plan.revoked_cookie} - to_revoke

    for entry in plan.revoked_online:
        logger.warning("reconcile_revoked_online", router_id=router_id, username=entry.get("user"), dry_run=dry_run)
    for cookie in plan.revoked_cookie:
        logger.warning("reconcile_revoked_cookie", router_id=router_id, username=cookie.get("user"), dry_run=dry_run)
    for session in plan.phantom_session:
        logger.warning("reconcile_phantom_session", router_id=router_id, session_row_id=str(session.id), username=session.username, dry_run=dry_run)

    if dry_run:
        stats["revoked_online"] += len(plan.revoked_online)
        stats["revoked_cookie"] += len(plan.revoked_cookie)
        stats["phantom_session"] += len(plan.phantom_session)
        return plan

    for name in to_revoke:
        result = await service.remove_hotspot_user_access(router_id, [name])
        stats["revoked_online"] += result.get("active_removed", 0)
        stats["revoked_cookie"] += result.get("cookies_cleared", 0)
    if cookie_only:
        stats["revoked_cookie"] += await service.clear_hotspot_cookies(router_id, sorted(cookie_only))

    if plan.phantom_session:
        await db.execute(
            update(Session)
            .where(Session.id.in_([s.id for s in plan.phantom_session]), Session.stopped_at.is_(None))
            .values(stopped_at=now, terminate_cause="reconcile-not-on-router")
        )
        stats["phantom_session"] += len(plan.phantom_session)
    return plan


async def reconcile_hotspot(ctx=None, *, dry_run: bool = False, router_ids: list[str] | None = None,
                            voucher_overrides: dict[str, str] | None = None) -> dict:
    stats = {
        "routers_checked": 0,
        "routers_skipped": 0,
        "router_errors": 0,
        "cookie_policy_applied": 0,
        "revoked_online": 0,
        "revoked_cookie": 0,
        "untracked_online": 0,
        "unknown_online": 0,
        "phantom_session": 0,
    }
    now = datetime.now(timezone.utc)
    service = MikroTikAPIService()

    async with async_session_factory() as db:
        query = (
            select(Router)
            .join(RouterCredential, RouterCredential.router_id == Router.id)
            .where(Router.is_active.is_(True))
        )
        if router_ids:
            query = query.where(Router.id.in_(router_ids))
        routers = (await db.execute(query)).scalars().all()

        for router in routers:
            if not router_is_reachable(router):
                stats["routers_skipped"] += 1
                continue
            try:
                await _reconcile_router(db, service, router, now, stats, dry_run, voucher_overrides)
                stats["routers_checked"] += 1
                if not dry_run:
                    await db.commit()
            except Exception as exc:
                await db.rollback()
                stats["router_errors"] += 1
                logger.warning("reconcile_router_failed", router_id=str(router.id), error=str(exc))

    logger.info("reconcile_hotspot_done", dry_run=dry_run, **stats)
    return stats
