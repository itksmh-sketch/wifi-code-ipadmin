"""
Job: invoice_generation — runs daily at 00:00 UTC.
Generates monthly invoices for active operators who don't have one for the current period.
"""
from __future__ import annotations
import logging
from calendar import monthrange
from datetime import datetime, timezone

import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.base import async_session_factory
from src.db.models import ISPOperator, OperatorInvoice
from src.modules.billing.service import create_invoice
from src.modules.notifications import dispatcher as notify

logger = structlog.get_logger(__name__)


async def _invoice_exists_for_period(
    db: AsyncSession, operator_id, period_start, period_end
) -> bool:
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import DATE
    result = await db.execute(
        select(OperatorInvoice).where(
            OperatorInvoice.isp_operator_id == operator_id,
            OperatorInvoice.period_start >= datetime(period_start.year, period_start.month, period_start.day, tzinfo=timezone.utc),
            OperatorInvoice.period_start < datetime(period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc),
            OperatorInvoice.status != "draft",
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def generate_monthly_invoices(ctx=None):
    """Daily job at 00:00 UTC."""
    now = datetime.now(timezone.utc)
    logger.info("invoice_generation_job_started", now=str(now))

    today = now.date()
    period_start = today.replace(day=1)
    last_day = monthrange(today.year, today.month)[1]
    period_end = today.replace(day=last_day)

    async with async_session_factory() as db:
        try:
            operators = (
                await db.execute(
                    select(ISPOperator).where(ISPOperator.billing_status == "active")
                )
            ).scalars().all()

            generated = 0
            for op in operators:
                try:
                    if await _invoice_exists_for_period(db, op.id, period_start, period_end):
                        continue

                    invoice = await create_invoice(db, op, period_start, period_end)
                    generated += 1

                    billing_url = f"{get_settings().platform_app_url}/admin/billing"
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

            await db.commit()
            logger.info("invoice_generation_job_completed", generated=generated)
        except Exception as exc:
            logger.error("invoice_generation_job_error error=%s", exc)
            await db.rollback()
