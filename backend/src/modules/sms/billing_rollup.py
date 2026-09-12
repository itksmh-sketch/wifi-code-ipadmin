"""Fold unbilled platform-gateway SMS usage into an operator's invoice.

Called once per operator, immediately after generate_monthly_invoices creates
their invoice for a new period — never for a period whose invoice already
existed (the job's own _invoice_exists_for_period check skips those before
this is ever reached, so a given usage record is only ever considered by one
invoice-creation call at a time).

"Unbilled" is sms_usage_records.invoice_line_item_id IS NULL — that FK is the
claim marker, not a separate boolean — combined with sent_at < this invoice's
period_start, with NO lower bound. Not "exactly the immediately prior
period": generate_monthly_invoices excludes suspended operators from its
operator query entirely, so if a rollup only looked at "last period" it would
permanently lose track of usage from any period an operator was skipped.
Leaving the window unbounded below means whenever an operator's next invoice
is eventually generated — one period late or several — every still-unclaimed
record gets swept in then, however old. A usage record written after this
cycle's query already ran (same period, later in the day/month) is simply
sent_at >= period_start and rolls forward untouched to the next cycle's run —
invoice_line_item_id stays NULL, so it is delayed, never lost.

A permanently-suspended operator who never reactivates leaves their
pre-suspension usage permanently unclaimed. That is the deliberate, accepted
outcome here — no invoice or debt was ever created for it (unlike an unpaid
invoice, which is a real receivable and gets an explicit waive), so there is
nothing to write off. Visibility into how much unbilled usage is sitting
idle is a natural extension of the (separately scoped, not yet built)
balance-delta reconciliation job, not something this function needs to solve.

Records are grouped by rate_ghs_per_segment, one invoice line item per group
— never blending records priced at different snapshotted rates into one
line, since a mid-period (or mid-suspension) rate change must show as
separate lines, not a re-priced average. Each line's amount is the exact sum
of its group's already-rounded amount_ghs values (see add_line_item's
amount_ghs override) — never a fresh quantity*unit_price recomputation.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import OperatorInvoice, SMSUsageRecord
from src.modules.billing.service import add_line_item

__all__ = ["roll_up_sms_usage"]


async def roll_up_sms_usage(
    db: AsyncSession,
    *,
    operator_id: uuid.UUID,
    invoice: OperatorInvoice,
    period_start: date,
) -> int:
    """Fold every unbilled sms_usage_records row from before period_start into
    `invoice`, one line item per distinct rate. Returns how many records were
    folded in — 0 is the common case, since most operators never use the
    platform gateway."""
    period_start_dt = datetime(period_start.year, period_start.month, period_start.day, tzinfo=timezone.utc)

    rows = (
        await db.execute(
            select(SMSUsageRecord).where(
                SMSUsageRecord.isp_operator_id == operator_id,
                SMSUsageRecord.invoice_line_item_id.is_(None),
                SMSUsageRecord.sent_at < period_start_dt,
                # A send made during internal testing is never billed. Flagged
                # rows stay unclaimed forever by design — that is the intended
                # end state for them, not a backlog for the stale-unbilled
                # report in jobs/sms_reconciliation to chase.
                SMSUsageRecord.is_diagnostic.is_(False),
            )
        )
    ).scalars().all()
    if not rows:
        return 0

    by_rate: dict[Decimal, list[SMSUsageRecord]] = defaultdict(list)
    for row in rows:
        by_rate[row.rate_ghs_per_segment].append(row)

    for sort_index, (rate, group) in enumerate(sorted(by_rate.items())):
        total_segments = sum(r.segment_count for r in group)
        total_amount = sum(r.amount_ghs for r in group)
        line = await add_line_item(
            db,
            invoice,
            kind="sms_usage",
            description=f"Platform SMS usage — {total_segments} segment(s) @ GHS {rate}/segment",
            quantity=Decimal(total_segments),
            unit_price_ghs=rate,
            amount_ghs=total_amount,
            metadata={"record_count": len(group)},
            sort_order=10 + sort_index,
        )
        for row in group:
            row.invoice_line_item_id = line.id

    return len(rows)
