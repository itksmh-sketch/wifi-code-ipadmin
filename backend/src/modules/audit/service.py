"""Writing audit rows.

``record`` adds the row to the caller's session and does not commit, so the
row and the change it describes commit or roll back together: no change
without its row, and no row for a change that never happened. A failed insert
fails the request. That is the intended trade: an unaudited change to
credentials or users is worse than a retry.

``record_detached`` is for the cases with no transaction to join: a request
that ends in an error response (a failed login), or an action whose effect is
outside the database (a router reboot). It commits in its own session and logs
rather than raises, because by then the outcome can no longer be undone.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import async_session_factory
from src.db.models import AuditLog
from src.modules.audit.context import Actor, RequestInfo, current_actor, current_request_info
from src.modules.audit.redact import jsonable, redact_detail

logger = logging.getLogger("audit")

# Distinguishes "not given" (take the actor's tenant) from an explicit None
# (a platform-wide action).
_FROM_ACTOR: Any = object()


def build(
    action: str,
    *,
    actor: Actor | None = None,
    isp_operator_id: uuid.UUID | None = _FROM_ACTOR,
    target_type: str | None = None,
    target_id: Any = None,
    summary: str | None = None,
    changes: dict | None = None,
    detail: dict | None = None,
    request_info: RequestInfo | None = None,
) -> AuditLog:
    actor = actor or current_actor() or Actor.system()
    info = request_info or current_request_info() or RequestInfo()
    if isp_operator_id is _FROM_ACTOR:
        isp_operator_id = actor.isp_operator_id
    return AuditLog(
        isp_operator_id=isp_operator_id,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
        actor_label=actor.label,
        action=action,
        target_type=target_type,
        target_id=None if target_id is None else str(target_id),
        summary=jsonable(summary) if summary is not None else None,
        # ``changes`` comes from redact.diff; redacting again costs nothing and
        # covers a caller that built it by hand.
        changes=redact_detail(changes) if changes else None,
        detail=redact_detail(detail) if detail else None,
        ip=info.ip,
        user_agent=info.user_agent,
        request_id=info.request_id,
    )


def record(db: AsyncSession, action: str, **kwargs: Any) -> AuditLog:
    """Add an audit row to ``db``. The caller commits."""
    row = build(action, **kwargs)
    db.add(row)
    return row


async def record_detached(action: str, **kwargs: Any) -> None:
    """Write an audit row in its own session. Never raises.

    When this runs as a background task, pass ``request_info`` and ``actor``
    captured at scheduling time rather than relying on the context.
    """
    try:
        async with async_session_factory() as db:
            db.add(build(action, **kwargs))
            await db.commit()
    except Exception as exc:  # the outcome it records has already happened
        logger.error("audit_write_failed action=%s error=%s", action, exc)
