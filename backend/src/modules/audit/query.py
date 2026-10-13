"""Listing audit rows for the two audit pages (operator and platform).

One query, two viewers. What differs is decided here, not in the routes:

  * scope: an operator's page is pinned to its own isp_operator_id, taken from
    the session, never from the request. No filter, cursor or parameter can
    widen it; every one of them only narrows.
  * presentation (read.present_actor): an operator never sees which platform
    owner acted, nor where from (ip, user agent), nor the free-text reason a
    platform owner typed when disabling sign-in. The platform view shows all.

Both default to human actors (operator users and platform owners);
``include_system`` adds the rows the system wrote on its own.

Pagination is keyset on (created_at, id), newest first, matching the
ix_audit_log_* indexes. The cursor is opaque to clients but carries only a
position, never a scope: a cursor made from another tenant's row just says
"older than this instant" and the tenant filter still applies.

The first page (no cursor) also carries ``total``, the number of rows the
filters match, counted up to COUNT_CAP and no further so a wide platform query
over two years of rows stays cheap; ``total_capped`` says the real number is
higher. Later pages omit it (None): the client keeps the first page's.
"""
from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AuditLog, ISPOperator
from src.modules.audit.context import OPERATOR_USER, PLATFORM_OWNER, SYSTEM
from src.modules.audit.read import VIEWER_OPERATOR, VIEWER_PLATFORM, changes_column, detail_column, present_actor

HUMAN_ACTORS = (OPERATOR_USER, PLATFORM_OWNER)
ACTOR_TYPES = (OPERATOR_USER, PLATFORM_OWNER, SYSTEM)
PAGE_DEFAULT = 50
PAGE_MAX = 200
COUNT_CAP = 10_000

# Typed by a platform owner when turning sign-in off or on (admin_accounts/
# access.py). Shown on the platform page only.
PLATFORM_FREE_TEXT_KEYS = frozenset({"reason"})


class BadQuery(ValueError):
    """A filter or cursor the client sent that can't be used (400)."""


@dataclass(frozen=True)
class Filters:
    operator_id: uuid.UUID | None = None      # platform page only
    platform_wide: bool = False               # platform page only: rows with no operator
    actor_type: str | None = None
    actor_id: uuid.UUID | None = None
    action_prefix: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    include_system: bool = False


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(row_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        stamp, row_id = json.loads(base64.urlsafe_b64decode(padded.encode()))
        created_at = datetime.fromisoformat(stamp)
        if created_at.tzinfo is None:
            raise ValueError("naive timestamp")
        return created_at, uuid.UUID(row_id)
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise BadQuery("Invalid cursor.") from exc


def _like_prefix(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


async def list_rows(
    db: AsyncSession,
    *,
    viewer: str,
    filters: Filters,
    tenant_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = PAGE_DEFAULT,
) -> dict[str, Any]:
    """One page of rows, newest first, the cursor for the next page (None on
    the last) and, on the first page only, the capped total. ``tenant_id`` is
    required for, and only used by, the operator viewer."""
    if viewer == VIEWER_OPERATOR and tenant_id is None:
        raise ValueError("an operator listing needs the tenant")
    if viewer not in (VIEWER_OPERATOR, VIEWER_PLATFORM):
        raise ValueError(f"unknown viewer {viewer!r}")
    limit = max(1, min(int(limit), PAGE_MAX))
    where = _conditions(viewer=viewer, filters=filters, tenant_id=tenant_id)

    q = select(
        AuditLog.id, AuditLog.created_at, AuditLog.isp_operator_id, AuditLog.actor_type, AuditLog.actor_id,
        AuditLog.actor_label, AuditLog.action, AuditLog.target_type, AuditLog.target_id, AuditLog.summary,
        changes_column(), detail_column(), AuditLog.ip, AuditLog.user_agent, ISPOperator.name.label("operator_name"),
    ).outerjoin(ISPOperator, ISPOperator.id == AuditLog.isp_operator_id).where(*where)
    if cursor:
        created_at, row_id = decode_cursor(cursor)
        q = q.where(tuple_(AuditLog.created_at, AuditLog.id) < tuple_(created_at, row_id))

    q = q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit + 1)
    fetched = (await db.execute(q)).all()
    page, more = fetched[:limit], len(fetched) > limit

    total = None
    if not cursor:
        matching = select(AuditLog.id).where(*where).limit(COUNT_CAP + 1).subquery()
        total = (await db.execute(select(func.count()).select_from(matching))).scalar_one()
    return {
        "rows": [present_row(row, viewer=viewer) for row in page],
        "next_cursor": encode_cursor(page[-1].created_at, page[-1].id) if more else None,
        "total": None if total is None else min(total, COUNT_CAP),
        "total_capped": total is not None and total > COUNT_CAP,
    }


def _conditions(*, viewer: str, filters: Filters, tenant_id: uuid.UUID | None) -> list:
    """The WHERE clauses for ``filters``, shared by the page and its count so
    the two can never disagree about which rows match."""
    where = []
    # Scope first. The operator page cannot name an operator at all.
    if viewer == VIEWER_OPERATOR:
        where.append(AuditLog.isp_operator_id == tenant_id)
    elif filters.platform_wide:
        where.append(AuditLog.isp_operator_id.is_(None))
    elif filters.operator_id is not None:
        where.append(AuditLog.isp_operator_id == filters.operator_id)

    if filters.actor_type is not None:
        if filters.actor_type not in ACTOR_TYPES:
            raise BadQuery("Unknown actor type.")
        where.append(AuditLog.actor_type == filters.actor_type)
    elif not filters.include_system:
        where.append(AuditLog.actor_type.in_(HUMAN_ACTORS))
    if filters.actor_id is not None:
        where.append(AuditLog.actor_id == filters.actor_id)
    if filters.action_prefix:
        where.append(AuditLog.action.like(_like_prefix(filters.action_prefix.strip()), escape="\\"))
    if filters.date_from is not None:
        where.append(AuditLog.created_at >= _day_start(filters.date_from))
    if filters.date_to is not None:
        where.append(AuditLog.created_at < _day_start(filters.date_to + timedelta(days=1)))
    return where


def present_row(row, *, viewer: str) -> dict[str, Any]:
    """What ``viewer`` is shown of one row."""
    from_platform = row.actor_type == PLATFORM_OWNER
    hide = viewer == VIEWER_OPERATOR and from_platform
    detail = row.detail
    if hide and isinstance(detail, dict):
        detail = {k: v for k, v in detail.items() if k not in PLATFORM_FREE_TEXT_KEYS} or None
    out = {
        "id": str(row.id),
        "created_at": row.created_at.isoformat(),
        "actor": present_actor(row.actor_type, row.actor_id, row.actor_label, viewer=viewer),
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "summary": row.summary,
        "changes": row.changes,
        "detail": detail,
        # Where a platform owner acted from is not the operator's business.
        "ip": None if hide or row.ip is None else str(row.ip),
        "user_agent": None if hide else row.user_agent,
    }
    if viewer == VIEWER_PLATFORM:
        out["operator"] = (
            {"id": str(row.isp_operator_id), "name": row.operator_name} if row.isp_operator_id else None
        )
    return out
