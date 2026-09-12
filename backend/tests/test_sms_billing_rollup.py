"""Unit tests for sms.billing_rollup.roll_up_sms_usage. No server, no real DB.

FakeRollupDb filters the SMSUsageRecord query LIVE against a shared list of
row objects (checking invoice_line_item_id is None), rather than returning a
pre-scripted queue — so running the rollup twice against the same rows is a
genuine idempotency check: the second run's query naturally excludes whatever
the first run just claimed, the same way the real `WHERE invoice_line_item_id
IS NULL` predicate would. add_line_item's own internal queries (flush, the
invoice-total recompute sum) are stubbed generically since this file isn't
re-testing billing/service.py's own correctness, only the rollup's.
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from src.db.models import OperatorInvoice, SMSUsageRecord
from src.modules.sms.billing_rollup import roll_up_sms_usage


class _Result:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def all(self):
        return self._value

    def scalar(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value


class FakeRollupDb:
    """`period_start_dt` mirrors the real query's `sent_at < period_start_dt`
    predicate explicitly, alongside the `invoice_line_item_id IS NULL` claim
    check -- both conditions applied here, not just the claim one, so a test
    asserting on "too-new usage isn't selected" is actually exercising that
    filter rather than passing by accident."""

    def __init__(self, usage_rows, period_start_dt):
        self.usage_rows = usage_rows
        self.period_start_dt = period_start_dt
        self.added_lines = []

    async def execute(self, statement):
        if "sms_usage_records" in str(statement):
            unclaimed = [
                r for r in self.usage_rows
                if r.invoice_line_item_id is None
                and r.sent_at < self.period_start_dt
                and not r.is_diagnostic
            ]
            return _Result(unclaimed)
        return _Result(Decimal("0"))  # recompute_invoice_total's sum -- unused by these tests

    def add(self, obj):
        if not getattr(obj, "id", None):
            obj.id = uuid.uuid4()
        self.added_lines.append(obj)

    async def flush(self):
        return None


def _invoice() -> OperatorInvoice:
    return OperatorInvoice(id=uuid.uuid4(), amount_ghs=Decimal("0.00"))


def _usage_row(*, segment_count, rate, amount, sent_at, claimed=False, diagnostic=False) -> SMSUsageRecord:
    return SMSUsageRecord(
        id=uuid.uuid4(),
        isp_operator_id="op-1",
        provider="arkesel",
        provider_reference=f"ref-{uuid.uuid4()}",
        segment_count=segment_count,
        rate_ghs_per_segment=rate,
        amount_ghs=amount,
        sent_at=sent_at,
        invoice_line_item_id=uuid.uuid4() if claimed else None,
        # Set explicitly: server_default only applies on a real DB insert, so an
        # unpersisted ORM object would otherwise carry None here.
        is_diagnostic=diagnostic,
    )


_PERIOD_START = date(2026, 4, 1)
_PERIOD_START_DT = datetime(2026, 4, 1, tzinfo=timezone.utc)
_BEFORE_PERIOD = datetime(2026, 3, 15, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_no_usage_adds_no_line_and_returns_zero():
    db = FakeRollupDb([], _PERIOD_START_DT)
    invoice = _invoice()
    count = await roll_up_sms_usage(db, operator_id="op-1", invoice=invoice, period_start=_PERIOD_START)
    assert count == 0
    assert db.added_lines == []


@pytest.mark.asyncio
async def test_multiple_records_same_rate_aggregate_into_one_line_summing_prerounded_amounts():
    # Rate chosen so per-record rounding (0.033 -> 0.03 each) differs from a
    # fresh quantity*unit_price recompute (3 * 0.033 = 0.099 -> 0.10): the
    # line must show the sum of the already-rounded records (0.09), not a
    # fresh recomputation (0.10) -- exactly the scenario add_line_item's
    # amount_ghs override exists for.
    rate = Decimal("0.0330")
    rows = [
        _usage_row(segment_count=1, rate=rate, amount=Decimal("0.03"), sent_at=_BEFORE_PERIOD)
        for _ in range(3)
    ]
    db = FakeRollupDb(rows, _PERIOD_START_DT)
    invoice = _invoice()

    count = await roll_up_sms_usage(db, operator_id="op-1", invoice=invoice, period_start=_PERIOD_START)

    assert count == 3
    assert len(db.added_lines) == 1
    line = db.added_lines[0]
    assert line.kind == "sms_usage"
    assert line.quantity == Decimal(3)
    assert line.unit_price_ghs == rate
    assert line.amount_ghs == Decimal("0.09")  # sum of records, NOT line_amount(3, 0.033) == 0.10
    assert all(r.invoice_line_item_id == line.id for r in rows)


@pytest.mark.asyncio
async def test_records_at_different_rates_produce_separate_lines():
    old_rate_rows = [
        _usage_row(segment_count=2, rate=Decimal("0.0300"), amount=Decimal("0.06"), sent_at=_BEFORE_PERIOD)
    ]
    new_rate_rows = [
        _usage_row(segment_count=1, rate=Decimal("0.0350"), amount=Decimal("0.04"), sent_at=_BEFORE_PERIOD),
        _usage_row(segment_count=1, rate=Decimal("0.0350"), amount=Decimal("0.04"), sent_at=_BEFORE_PERIOD),
    ]
    db = FakeRollupDb(old_rate_rows + new_rate_rows, _PERIOD_START_DT)
    invoice = _invoice()

    count = await roll_up_sms_usage(db, operator_id="op-1", invoice=invoice, period_start=_PERIOD_START)

    assert count == 3
    assert len(db.added_lines) == 2
    by_rate = {line.unit_price_ghs: line for line in db.added_lines}
    assert by_rate[Decimal("0.0300")].quantity == Decimal(2)
    assert by_rate[Decimal("0.0300")].amount_ghs == Decimal("0.06")
    assert by_rate[Decimal("0.0350")].quantity == Decimal(2)
    assert by_rate[Decimal("0.0350")].amount_ghs == Decimal("0.08")  # 0.04 + 0.04, summed not recomputed


@pytest.mark.asyncio
async def test_already_claimed_records_are_never_reselected():
    claimed = _usage_row(segment_count=1, rate=Decimal("0.03"), amount=Decimal("0.03"), sent_at=_BEFORE_PERIOD, claimed=True)
    unclaimed = _usage_row(segment_count=1, rate=Decimal("0.03"), amount=Decimal("0.03"), sent_at=_BEFORE_PERIOD)
    db = FakeRollupDb([claimed, unclaimed], _PERIOD_START_DT)
    invoice = _invoice()

    count = await roll_up_sms_usage(db, operator_id="op-1", invoice=invoice, period_start=_PERIOD_START)

    assert count == 1  # only the unclaimed row
    assert len(db.added_lines) == 1
    assert db.added_lines[0].quantity == Decimal(1)


@pytest.mark.asyncio
async def test_rerun_is_idempotent_second_run_finds_nothing_left_to_claim():
    rows = [
        _usage_row(segment_count=1, rate=Decimal("0.03"), amount=Decimal("0.03"), sent_at=_BEFORE_PERIOD)
        for _ in range(2)
    ]
    db = FakeRollupDb(rows, _PERIOD_START_DT)
    invoice = _invoice()

    first_count = await roll_up_sms_usage(db, operator_id="op-1", invoice=invoice, period_start=_PERIOD_START)
    assert first_count == 2
    assert len(db.added_lines) == 1
    first_line_id = db.added_lines[0].id

    # Same db, same underlying rows, no new usage in between -- a second run
    # (e.g. a retried or re-triggered job) must claim nothing new and must
    # not create a second line item or reassign the rows' claim.
    second_count = await roll_up_sms_usage(db, operator_id="op-1", invoice=invoice, period_start=_PERIOD_START)
    assert second_count == 0
    assert len(db.added_lines) == 1  # no new line added
    assert all(r.invoice_line_item_id == first_line_id for r in rows)


@pytest.mark.asyncio
async def test_diagnostic_records_are_never_billed():
    # A test send still exists and still burned real provider credits, but must
    # never reach an invoice -- it stays unclaimed permanently, by design.
    billable = _usage_row(segment_count=1, rate=Decimal("0.30"), amount=Decimal("0.30"), sent_at=_BEFORE_PERIOD)
    diagnostic = _usage_row(
        segment_count=1, rate=Decimal("0.30"), amount=Decimal("0.30"),
        sent_at=_BEFORE_PERIOD, diagnostic=True,
    )
    db = FakeRollupDb([billable, diagnostic], _PERIOD_START_DT)
    invoice = _invoice()

    count = await roll_up_sms_usage(db, operator_id="op-1", invoice=invoice, period_start=_PERIOD_START)

    assert count == 1
    assert len(db.added_lines) == 1
    assert db.added_lines[0].quantity == Decimal(1)
    assert db.added_lines[0].amount_ghs == Decimal("0.30")
    assert billable.invoice_line_item_id is not None
    assert diagnostic.invoice_line_item_id is None  # never claimed


@pytest.mark.asyncio
async def test_only_diagnostic_usage_produces_no_line_at_all():
    diagnostic = _usage_row(
        segment_count=2, rate=Decimal("0.30"), amount=Decimal("0.60"),
        sent_at=_BEFORE_PERIOD, diagnostic=True,
    )
    db = FakeRollupDb([diagnostic], _PERIOD_START_DT)
    invoice = _invoice()

    count = await roll_up_sms_usage(db, operator_id="op-1", invoice=invoice, period_start=_PERIOD_START)

    assert count == 0
    assert db.added_lines == []
    assert diagnostic.invoice_line_item_id is None


@pytest.mark.asyncio
async def test_new_usage_after_this_cycle_rolls_forward_to_next_run():
    # A record written after this cycle's window (sent_at >= period_start) is
    # simply not selected -- it stays unclaimed for whenever the NEXT
    # invoice's rollup runs, rather than being lost.
    in_window = _usage_row(segment_count=1, rate=Decimal("0.03"), amount=Decimal("0.03"), sent_at=_BEFORE_PERIOD)
    too_new = _usage_row(
        segment_count=1, rate=Decimal("0.03"), amount=Decimal("0.03"),
        sent_at=datetime(2026, 4, 5, tzinfo=timezone.utc),  # on/after period_start (2026-04-01)
    )
    db = FakeRollupDb([in_window, too_new], _PERIOD_START_DT)
    invoice = _invoice()

    count = await roll_up_sms_usage(db, operator_id="op-1", invoice=invoice, period_start=_PERIOD_START)

    assert count == 1
    assert in_window.invoice_line_item_id is not None
    assert too_new.invoice_line_item_id is None  # left for the next cycle
