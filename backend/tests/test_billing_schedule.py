"""When monthly billing starts after a trial (billing.service.first_invoice_date,
trial_covers_period). Pure unit tests: no DB."""
from __future__ import annotations

import types
from datetime import date, datetime, timedelta, timezone

import pytest

from src.modules.billing.service import first_invoice_date, trial_covers_period

UTC = timezone.utc


@pytest.mark.parametrize("trial_ends_at,expected", [
    (datetime(2026, 10, 20, 9, 0, tzinfo=UTC), date(2026, 11, 1)),
    (datetime(2026, 10, 1, 0, 0, tzinfo=UTC), date(2026, 11, 1)),       # ends on the 1st: next month still
    (datetime(2026, 10, 31, 23, 59, tzinfo=UTC), date(2026, 11, 1)),
    (datetime(2026, 12, 15, tzinfo=UTC), date(2027, 1, 1)),             # across the year end
    (datetime(2026, 2, 28, tzinfo=UTC), date(2026, 3, 1)),
    # Ghana is UTC: a non-UTC timestamp is read as its UTC day.
    (datetime(2026, 11, 1, 0, 30, tzinfo=timezone(timedelta(hours=1))), date(2026, 11, 1)),
])
def test_billing_starts_on_the_first_of_the_month_after_the_trial(trial_ends_at, expected):
    assert first_invoice_date(trial_ends_at) == expected


def _op(trial_ends_at):
    return types.SimpleNamespace(trial_ends_at=trial_ends_at)


def test_a_month_the_trial_ran_into_is_not_billed():
    op = _op(datetime(2026, 10, 20, 9, 0, tzinfo=UTC))
    assert trial_covers_period(op, date(2026, 10, 1)) is True
    assert trial_covers_period(op, date(2026, 11, 1)) is False
    assert trial_covers_period(op, date(2026, 12, 1)) is False


def test_an_operator_without_a_trial_is_billed_from_the_first_month():
    assert trial_covers_period(_op(None), date(2026, 10, 1)) is False


def test_a_trial_extended_into_next_month_moves_billing_with_it():
    op = _op(datetime(2026, 11, 3, tzinfo=UTC))
    assert trial_covers_period(op, date(2026, 11, 1)) is True
    assert first_invoice_date(op.trial_ends_at) == date(2026, 12, 1)
