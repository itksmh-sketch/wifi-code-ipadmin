"""Reading audit rows: the two rules every reader must follow.

1. The JSON value null means "absent", exactly like SQL NULL. Rows written
   before JSONB(none_as_null=True) stored it (migration 056 converted them),
   and the guard stays so no reader ever depends on that conversion.
   ``changes_column()`` / ``detail_column()`` are what a query selects or
   filters on instead of the raw columns. (Fetched values need no rule:
   asyncpg already returns JSON null as None. It is the SQL side — a filter
   such as "changes IS NULL" — that would otherwise miss rows.)

2. An operator never sees which platform owner acted: platform rows show as
   "Platform", with no email or id. ``present_actor`` is the one place that
   decides what a viewer is shown.
"""
from __future__ import annotations

from sqlalchemy import func, literal_column

from src.db.models import AuditLog
from src.modules.audit.context import OPERATOR_USER, PLATFORM_OWNER, SYSTEM

# A SQL literal, not a bound parameter: binding the string "null" as JSONB
# would serialise it to the JSON *string* "null", which never matches.
_JSON_NULL = literal_column("'null'::jsonb")

PLATFORM_LABEL = "Platform"
SYSTEM_LABEL = "System"

VIEWER_OPERATOR = "operator"
VIEWER_PLATFORM = "platform"


def null_safe(column):
    """``column``, with the JSON value null read as SQL NULL."""
    return func.nullif(column, _JSON_NULL)


def changes_column():
    return null_safe(AuditLog.changes).label("changes")


def detail_column():
    return null_safe(AuditLog.detail).label("detail")


def present_actor(actor_type: str, actor_id, actor_label: str | None, *, viewer: str) -> dict:
    """What ``viewer`` is shown as the actor of one row."""
    if actor_type == PLATFORM_OWNER and viewer == VIEWER_OPERATOR:
        return {"type": PLATFORM_OWNER, "id": None, "label": PLATFORM_LABEL}
    if actor_type == SYSTEM:
        return {"type": SYSTEM, "id": None, "label": SYSTEM_LABEL}
    if actor_type in (OPERATOR_USER, PLATFORM_OWNER):
        return {"type": actor_type, "id": str(actor_id) if actor_id else None, "label": actor_label}
    raise ValueError(f"Unknown actor_type: {actor_type}")
