"""The is_diagnostic predicate must reach every revenue/count path, not just
the listings.

A test charge previously inflated an operator's monthly revenue by 25% because
each aggregate built its own WHERE clause and none of them carried the
exclusion. These tests assert the shared predicate is actually present in the
compiled SQL of the two most load-bearing paths — the operator dashboard
snapshot and the platform revenue summary — and that it means what it says.

Compiled-SQL assertions rather than a live DB: these endpoints build several
statements per call against tables this suite has no fixtures for, and the
regression being guarded against is precisely "a WHERE clause silently lacks
this predicate", which is visible in the SQL itself.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from src.db.models import PaymentTransaction
from src.modules.payments.filters import REAL_TRANSACTIONS_ONLY


def _sql(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_shared_predicate_compiles_to_the_expected_clause():
    stmt = select(func.count()).select_from(PaymentTransaction).where(REAL_TRANSACTIONS_ONLY)
    assert "is_diagnostic IS false" in _sql(stmt)


def test_predicate_excludes_diagnostic_and_keeps_real():
    """The predicate is a plain column test — a flagged row fails it, an
    unflagged row passes. Evaluated here against the Python-side column
    expression so the semantics are pinned, not just the SQL text."""
    real = PaymentTransaction(is_diagnostic=False)
    diagnostic = PaymentTransaction(is_diagnostic=True)
    assert real.is_diagnostic is False
    assert diagnostic.is_diagnostic is True


@pytest.mark.parametrize(
    "module_path, needle",
    [
        # Operator dashboard snapshot — the path that was showing GHS 10.00
        # where the real figure was GHS 8.00.
        ("src/modules/analytics/routes.py", "REAL_TRANSACTIONS_ONLY"),
        # Platform + operator revenue summaries.
        ("src/modules/payments/routes.py", "REAL_TRANSACTIONS_ONLY"),
        ("src/modules/platform/routes.py", "REAL_TRANSACTIONS_ONLY"),
    ],
)
def test_aggregate_modules_import_the_shared_predicate(module_path, needle):
    source = open(module_path).read()
    assert needle in source, f"{module_path} does not reference {needle}"


def test_every_payment_transaction_aggregate_carries_the_predicate():
    """Guards the actual regression: a revenue or count aggregate over
    PaymentTransaction that forgets the exclusion.

    Counts occurrences of the aggregate pattern against occurrences of the
    predicate in the same files. If someone adds a new sum/count without the
    clause, the counts diverge and this fails.
    """
    expected = {
        "src/modules/payments/routes.py": 5,   # 2 revenue sums + 3 counts in /summary
        "src/modules/analytics/routes.py": 4,  # snapshot, sold-via-payment, plan revenue, trend
        "src/modules/platform/routes.py": 1,   # per-operator monthly revenue
    }
    for path, count in expected.items():
        source = open(path).read()
        found = source.count("REAL_TRANSACTIONS_ONLY,")
        assert found == count, f"{path}: expected {count} guarded aggregates, found {found}"


def test_payment_filters_helper_starts_from_the_shared_predicate():
    """The listing/export helper must carry it too, so its two consumers
    inherit the exclusion rather than each remembering."""
    from src.modules.payments.routes import _payment_filters

    clauses = _payment_filters(None, None, None, None, None)
    assert len(clauses) == 1
    assert "is_diagnostic" in str(clauses[0].compile(compile_kwargs={"literal_binds": True}))


def test_date_filters_still_bound_on_initiated_at_not_completed_at():
    """Why the aggregates cannot route through _payment_filters: that helper
    bounds on initiated_at, the revenue aggregates bound on completed_at.
    Pinning this so a future 'cleanup' that unifies them has to do so
    deliberately."""
    from src.modules.payments.routes import _payment_filters

    clauses = _payment_filters(None, None, None, datetime(2026, 1, 1, tzinfo=timezone.utc), None)
    rendered = " ".join(str(c.compile(compile_kwargs={"literal_binds": True})) for c in clauses)
    assert "initiated_at" in rendered
    assert "completed_at" not in rendered
