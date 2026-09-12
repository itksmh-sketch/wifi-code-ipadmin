"""One definition of which payment transactions count as real.

A transaction flagged is_diagnostic is an internal test artifact — it must
never appear in an operator's history, revenue, or counts, nor in the
platform's per-operator revenue. Before this module that clause lived nowhere
and every aggregate independently forgot it, which is how a test charge ended
up inflating an operator's monthly revenue by 25%.

Imported rather than retyped so there is a single place to change if the rule
ever grows (a voided status, say), instead of ten queries each remembering.

Deliberately NOT folded into payments.routes._payment_filters: that helper
bounds dates on initiated_at, while every revenue aggregate bounds on
completed_at. Routing the aggregates through it would silently change which
timestamp defines a period — a behaviour change wearing a refactor's clothes.
The shared piece is this predicate; the shapes around it legitimately differ.
"""
from __future__ import annotations

from src.db.models import PaymentTransaction

__all__ = ["REAL_TRANSACTIONS_ONLY"]

# A SQLAlchemy expression is immutable, so one module-level instance is safe to
# reuse across every query.
REAL_TRANSACTIONS_ONLY = PaymentTransaction.is_diagnostic.is_(False)
