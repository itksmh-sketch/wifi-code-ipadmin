"""
Job: invoice_generation — runs daily at 00:00 UTC.
Generates monthly invoices for active operators who don't have one for the current period.

- Billing starts on the 1st of the month after a trial ends: a month the trial
  ran into is never invoiced (service.trial_covers_period). The trial's end
  issues no invoice either (jobs/trial_expiry), so there is one invoice per
  month and never two within days of each other.
- Every invoice takes the operator's unbilled platform SMS usage from before
  the period (sms.billing_rollup), so usage during the trial lands on the first
  monthly invoice.
- An operator on a GHS 0 fee gets an SMS-only invoice once that unbilled usage
  reaches GHS 1 (Paystack's minimum charge); below that it carries forward to
  the next month untouched.
"""
from __future__ import annotations
import logging
from calendar import monthrange
from datetime import datetime, timezone

import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import async_session_factory
from src.db.models import ISPOperator, OperatorInvoice
from src.modules.billing.service import (
    PAYSTACK_MINIMUM_GHS, create_invoice, record_invoice_issued, trial_covers_period,
)
from src.modules.notifications import dispatcher as notify
from src.modules.sms.billing_rollup import roll_up_sms_usage, unbilled_sms_total

logger = structlog.get_logger(__name__)


async def _invoice_exists_for_period(
    db: AsyncSession, operator_id, period_start, period_end
) -> bool:
    result = await db.execute(
        select(OperatorInvoice).where(
            OperatorInvoice.isp_operator_id == operator_id,
            OperatorInvoice.period_start >= datetime(period_start.year, period_start.month, period_start.day, tzinfo=timezone.utc),
            OperatorInvoice.period_start < datetime(period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc),
            OperatorInvoice.status != "draft",
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def generate_monthly_invoices(ctx=None, *, now: datetime | None = None):
    """Daily job at 00:00 UTC. `now` is for tests: the run as of that moment."""
    now = now or datetime.now(timezone.utc)
    logger.info("invoice_generation_job_started", now=str(now))

    today = now.date()
    period_start = today.replace(day=1)
    last_day = monthrange(today.year, today.month)[1]
    period_end = today.replace(day=last_day)

    async with async_session_factory() as db:
        try:
            # Every active operator: those on a fee, and those on GHS 0 who may
            # owe for SMS. A fee between 0 and GHS 1 can't exist (the
            # isp_operators CHECK), so "not on a fee" means exactly 0.
            operators = (
                await db.execute(select(ISPOperator).where(ISPOperator.billing_status == "active"))
            ).scalars().all()

            generated = 0
            carried_forward = []
            for op in operators:
                try:
                    if trial_covers_period(op, period_start):
                        continue
                    if await _invoice_exists_for_period(db, op.id, period_start, period_end):
                        continue

                    if op.monthly_fee_ghs < PAYSTACK_MINIMUM_GHS:
                        # Billable and payable are the same threshold: below
                        # Paystack's floor an invoice can't be charged, would run
                        # through the grace period and suspend the operator.
                        sms_owed = await unbilled_sms_total(db, operator_id=op.id, period_start=period_start)
                        if sms_owed < PAYSTACK_MINIMUM_GHS:
                            if sms_owed > 0:
                                carried_forward.append({"operator": op.slug, "sms_ghs": str(sms_owed)})
                            continue

                    invoice = await create_invoice(db, op, period_start, period_end)
                    # Before the notify call, so the notified amount includes
                    # any rolled-up SMS usage, not just the subscription line.
                    await roll_up_sms_usage(
                        db, operator_id=op.id, invoice=invoice, period_start=period_start
                    )
                    record_invoice_issued(db, invoice, via="monthly")
                    generated += 1

                    billing_url = await notify.app_url("/admin/billing")
                    try:
                        await notify.notify_invoice_issued(
                            email=op.contact_email,
                            phone=op.contact_phone or "",
                            isp_name=op.name,
                            invoice_number=invoice.invoice_number,
                            amount_ghs=invoice.amount_ghs,
                            period_start=period_start.strftime("%B %d"),
                            period_end=period_end.strftime("%B %d, %Y"),
                            due_date=invoice.due_at.strftime("%B %d, %Y") if invoice.due_at else "",
                            payment_url=billing_url,
                        )
                    except Exception as exc:
                        logger.error("invoice_notify_error operator=%s error=%s", op.slug, exc)

                except Exception as exc:
                    logger.error("invoice_generation_operator_error operator=%s error=%s", op.slug, exc)

            if carried_forward:
                logger.info("invoice_generation_sms_carried_forward", operators=carried_forward,
                            count=len(carried_forward))
            await db.commit()
            logger.info("invoice_generation_job_completed", generated=generated)
        except Exception as exc:
            logger.error("invoice_generation_job_error error=%s", exc)
            await db.rollback()
