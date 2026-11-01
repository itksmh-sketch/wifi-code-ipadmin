"""
Job: trial_expiry â€” runs daily at 06:00 UTC.
- Sends warning 3 days before trial ends (idempotent: checks billing_events).
- Transitions expired trials to 'active'. No invoice here: billing starts on
  the 1st of the following month, when generate_monthly_invoices issues the
  first one (with any SMS used during the trial). Invoicing here as well put
  two invoices days apart on an operator whose trial ended late in a month.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.base import async_session_factory
from src.db.models import ISPOperator, OperatorBillingEvent, OperatorInvoice
from src.modules import audit
from src.modules.audit.fields import OPERATOR_STATUS
from src.modules.billing.service import first_invoice_date
from src.modules.notifications import dispatcher as notify

logger = structlog.get_logger(__name__)


async def _already_sent(db: AsyncSession, operator_id, event_type: str) -> bool:
    """Check if a billing event of this type was sent in the last 7 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    result = await db.execute(
        select(OperatorBillingEvent).where(
            OperatorBillingEvent.isp_operator_id == operator_id,
            OperatorBillingEvent.event_type == event_type,
            OperatorBillingEvent.created_at >= cutoff,
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def handle_trial_expiry(ctx=None, *, now: datetime | None = None):
    """Daily job at 06:00 UTC. `now` is for tests: the run as of that moment."""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    logger.info("trial_expiry_job_started", now=str(now))

    async with async_session_factory() as db:
        try:
            operators = (
                await db.execute(
                    select(ISPOperator).where(ISPOperator.billing_status == "trial")
                )
            ).scalars().all()

            for op in operators:
                if not op.trial_ends_at:
                    continue
                try:
                    delta = op.trial_ends_at - now
                    days_left = delta.days

                    if days_left <= 3 and days_left > 0:
                        if not await _already_sent(db, op.id, "trial_expiry_warning"):
                            event = OperatorBillingEvent(
                                isp_operator_id=op.id,
                                event_type="trial_expiry_warning",
                                description=f"Trial expiry warning sent. {days_left} days remaining.",
                                event_metadata={"days_remaining": days_left},
                            )
                            db.add(event)
                            await db.flush()
                            try:
                                await notify.notify_trial_expiry_warning(
                                    email=op.contact_email,
                                    phone=op.contact_phone or "",
                                    isp_name=op.name,
                                    trial_end_date=op.trial_ends_at.strftime("%B %d, %Y"),
                                    days_remaining=days_left,
                                    monthly_fee_ghs=op.monthly_fee_ghs,
                                    first_invoice_date=first_invoice_date(op.trial_ends_at).strftime("%B %d, %Y"),
                                )
                            except Exception as exc:
                                logger.error("trial_warning_notify_error operator=%s error=%s", op.slug, exc)

                    elif op.trial_ends_at <= now:
                        # Trial over: billing goes active. The first invoice
                        # comes on first_invoice_date, from the monthly run.
                        before = OPERATOR_STATUS.snapshot(op)
                        op.billing_status = "active"
                        billing_starts = first_invoice_date(op.trial_ends_at)
                        audit.record_system(
                            db, "operator.trial_end", isp_operator_id=op.id, target_type="isp_operator",
                            target_id=op.id, summary=f"Trial ended for {op.name}",
                            changes=OPERATOR_STATUS.diff(before, OPERATOR_STATUS.snapshot(op)),
                            detail={"trial_ends_at": op.trial_ends_at, "first_invoice_date": billing_starts},
                        )

                        event = OperatorBillingEvent(
                            isp_operator_id=op.id,
                            event_type="trial_expired",
                            description=f"Trial expired for {op.name}. Billing starts {billing_starts:%d %b %Y}.",
                            event_metadata={"first_invoice_date": billing_starts.isoformat()},
                        )
                        db.add(event)

                        try:
                            await notify.notify_trial_expired(
                                email=op.contact_email,
                                phone=op.contact_phone or "",
                                isp_name=op.name,
                                first_invoice_date=billing_starts.strftime("%B %d, %Y"),
                            )
                        except Exception as exc:
                            logger.error("trial_expired_notify_error operator=%s error=%s", op.slug, exc)

                        logger.info("trial_expired operator=%s first_invoice_date=%s", op.slug, billing_starts)

                except Exception as exc:
                    logger.error("trial_expiry_operator_error operator=%s error=%s", op.slug, exc)

            await db.commit()
            logger.info("trial_expiry_job_completed")
        except Exception as exc:
            logger.error("trial_expiry_job_error error=%s", exc)
            await db.rollback()
