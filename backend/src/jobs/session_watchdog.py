"""Force-close sessions that stopped reporting, independent of session identity.

Why this exists
---------------
Second layer under the router-scoped session-id fix, not a replacement for it.
Every other close path depends on the router telling us something: an Acct-Stop,
or a reconcile pass that reaches the router's API. When neither arrives, a row
stays open forever. Two consequences, both observed in production on
2026-09-16: the voucher keeps looking online, and the open row remains a
permanent collision target for any future session that reuses its id (an
89-day-old Aflao row was closed by a September reconnect on a different router).

Tiers
-----
``stale``    — no interim update for STALE_AFTER. Accounting-Interim-Interval is
               60s (see freeradius/sql.conf authorize_reply), so this is ~30
               missed interims. Guarded: skipped entirely for routers that are
               themselves unreachable, because a WireGuard outage stops every
               interim from a site at once and would otherwise mass-close a
               whole site's live sessions.
``backstop`` — started longer ago than BACKSTOP_AFTER (max plan duration is
               1440 min, so 25h is past anything legitimate). Unconditional: no
               router-health guard, because nothing genuinely runs this long.

Closing a row does not discard usage — update_voucher_usage sums every row for
a voucher, so the bytes already recorded survive.
"""
from __future__ import annotations

from sqlalchemy import text

from src.db.base import async_session_factory

import structlog

logger = structlog.get_logger(__name__)

# No interim for this long => the session is not really running.
STALE_AFTER = "30 minutes"
# ...but only trust that if the router itself has checked in recently.
ROUTER_HEALTHY_WITHIN = "10 minutes"
# Longer than any plan can legitimately run (max duration_minutes is 1440).
BACKSTOP_AFTER = "25 hours"

_STALE_SQL = text(
    """
    UPDATE sessions s
    SET stopped_at = NOW(),
        terminate_cause = 'watchdog-stale'
    FROM routers r
    WHERE r.id = s.router_id
      AND s.stopped_at IS NULL
      AND COALESCE(s.last_interim_at, s.started_at) < NOW() - CAST(:stale_after AS interval)
      -- WireGuard-outage guard: only close when the router is demonstrably
      -- alive and simply not accounting for THIS session.
      AND r.last_seen_at IS NOT NULL
      AND r.last_seen_at > NOW() - CAST(:router_window AS interval)
    RETURNING s.id, s.session_id, r.name
    """
)

_BACKSTOP_SQL = text(
    """
    UPDATE sessions s
    SET stopped_at = NOW(),
        terminate_cause = 'watchdog-max-duration'
    FROM routers r
    WHERE r.id = s.router_id
      AND s.stopped_at IS NULL
      AND s.started_at < NOW() - CAST(:backstop_after AS interval)
    RETURNING s.id, s.session_id, r.name
    """
)

_COLUMN_CHECK = text(
    "SELECT 1 FROM information_schema.columns "
    "WHERE table_name = 'sessions' AND column_name = 'last_interim_at'"
)


async def session_watchdog(ctx=None) -> dict:
    stats = {"stale_closed": 0, "backstop_closed": 0, "skipped_no_column": False}

    async with async_session_factory() as db:
        # Migration 045 adds last_interim_at. Until it lands this job is a no-op
        # rather than an error every 15 minutes, so it can ship independently of
        # the session-identity fix.
        if (await db.execute(_COLUMN_CHECK)).scalar_one_or_none() is None:
            stats["skipped_no_column"] = True
            logger.info("session_watchdog_skipped_pending_migration", module=__name__, **stats)
            return stats

        stale = (
            await db.execute(
                _STALE_SQL, {"stale_after": STALE_AFTER, "router_window": ROUTER_HEALTHY_WITHIN}
            )
        ).all()
        backstop = (
            await db.execute(_BACKSTOP_SQL, {"backstop_after": BACKSTOP_AFTER})
        ).all()
        await db.commit()

    stats["stale_closed"] = len(stale)
    stats["backstop_closed"] = len(backstop)

    for row in stale:
        logger.warning(
            "session_watchdog_closed_stale",
            module=__name__, session_id=row.session_id, router=row.name,
        )
    for row in backstop:
        logger.warning(
            "session_watchdog_closed_backstop",
            module=__name__, session_id=row.session_id, router=row.name,
        )

    logger.info("session_watchdog_completed", module=__name__, **stats)
    return stats
