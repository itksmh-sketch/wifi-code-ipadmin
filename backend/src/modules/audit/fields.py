"""Per-model field lists for audit ``changes``.

One entry per audited model, in one place, so what a row may say about a model
is decided once rather than at every call site:

  * ``tracked``: the columns a change row looks at. A column not listed here
    never appears in ``changes`` at all, so a new column is invisible until
    someone adds it on purpose.
  * ``shown``: the tracked columns whose before/after values are written.
    Every other tracked column that changed is recorded as "changed".

redact.diff still applies on top: a secret-looking name is never shown even if
listed (tests/test_audit_fields.py also refuses to let one be listed).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.modules.audit.redact import diff, snapshot


@dataclass(frozen=True)
class ModelFields:
    model: str
    tracked: tuple[str, ...]
    shown: frozenset[str]

    def snapshot(self, obj: Any) -> dict[str, Any] | None:
        return None if obj is None else snapshot(obj, self.tracked)

    def diff(self, before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any] | None:
        return diff(before, after, shown=self.shown)


def _fields(model: str, tracked: tuple[str, ...], hidden: tuple[str, ...] = ()) -> ModelFields:
    return ModelFields(model, tracked, frozenset(tracked) - frozenset(hidden))


PLAN = _fields("Plan", (
    "name", "type", "site_id", "duration_minutes", "data_limit_mb",
    "download_speed_kbps", "upload_speed_kbps", "price_ghs", "is_active",
))

# Never code, username or password: a voucher is bearer credit.
VOUCHER = _fields("Voucher", ("status", "printed_at"))

ALL = (PLAN, VOUCHER)
