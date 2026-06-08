"""
Job: billing_enforcement â€” runs daily at 08:00 UTC.
1. Marks issued invoices as overdue when past due_at.
2. Suspends operators whose overdue invoice is past the grace period.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.base import async_session_factory
from src.db.models import ISPOperator, OperatorInvoice, OperatorBillingEvent
from src.modules.notifications import dispatcher as notify

logger = structlog.get_logger(__name__)


async def _grace_warning_sent(db: AsyncSession, operator_id, invoice_id) -> bool:
    result = await db.execute(
        select(OperatorBillingEvent).where(
            OperatorBillingEvent.isp_operator_id == operator_id,
            OperatorBillingEvent.event_type == "grace_period_started",
            OperatorBillingEvent.event_metadata["invoice_id"].astext == str(invoice_id),
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def enforce_billing(ctx=None):
    """Daily job at 08:00 UTC."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    logger.info("billing_enforcement_job_started", now=str(now))

    async with async_session_factory() as db:
        try:
            # Step 1: Issued invoices past due_at â†’ overdue + grace period warning
            issued_overdue = (
                await db.execute(
                    select(OperatorInvoice).where(
                        OperatorInvoice.status == "issued",
                        OperatorInvoice.due_at < now,
                    )
                )
            ).scalars().all()

            for invoice in issued_overdue:
                try:
                    invoice.status = "overdue"
                    event = OperatorBillingEvent(
                        isp_operator_id=invoice.isp_operator_id,
                        event_type="invoice_overdue",
                        description=f"Invoice {invoice.invoice_number} marked overdue.",
                        event_metadata={"invoice_id": str(invoice.id), "invoice_number": invoice.invoice_number},
                    )
                    db.add(event)

                    # Send grace period warning (once per invoice)
                    if not await _grace_warning_sent(db, invoice.isp_operator_id, invoice.id):
                        suspension_date = (invoice.due_at + timedelta(days=settings.grace_period_days)).strftime("%B %d, %Y")
                        grace_event = OperatorBillingEvent(
                            isp_operator_id=invoice.isp_operator_id,
                            event_type="grace_period_started",
                            description=f"Grace period started for invoice {invoice.invoice_number}.",
                            event_metadata={"invoice_id": str(invoice.id), "invoice_number": invoice.invoice_number},
                        )
                        db.add(grace_event)

                        op = await db.get(ISPOperator, invoice.isp_operator_id)
                        if op:
                            billing_url = f"{settings.platform_app_url}/admin/billing"
                            try:
                                await notify.notify_grace_period(
                                    email=op.contact_email,
                                    phone=op.contact_phone or "",
                                    isp_name=op.name,
                                    invoice_number=invoice.invoice_number,
                                    amount_ghs=invoice.amount_ghs,
                                    suspension_date=suspension_date,
                                    payment_url=billing_url,
                                )
                            except Exception as exc:
                                logger.error("grace_notify_error operator=%s error=%s", op.slug, exc)

                except Exception as exc:
                    logger.error("overdue_processing_error invoice=%s error=%s", invoice.invoice_number, exc)

            # Step 2: Overdue invoices past grace period â†’ suspend operator
            grace_deadline = now - timedelta(days=settings.grace_period_days)
            suspension_candidates = (
                await db.execute(
                    select(OperatorInvoice).where(
                        OperatorInvoice.status == "overdue",
                        OperatorInvoice.due_at < grace_deadline,
                    )
                )
            ).scalars().all()

            for invoice in suspension_candidates:
                try:
                    op = await db.get(ISPOperator, invoice.isp_operator_id)
                    if not op or op.status == "suspended":
                        continue

                    op.status = "suspended"
                    op.billing_status = "past_due"

                    event = OperatorBillingEvent(
                        isp_operator_id=op.id,
                        event_type="suspended",
                        description=f"{op.name} suspended due to non-payment of {invoice.invoice_number}.",
                        event_metadata={"invoice_id": str(invoice.id), "invoice_number": invoice.invoice_number},
                    )
                    db.add(event)

                    try:
                        await notify.notify_suspended(
                            email=op.contact_email,
                            phone=op.contact_phone or "",
                            isp_name=op.name,
                        )
                    except Exception as exc:
                        logger.error("suspend_notify_error operator=%s error=%s", op.slug, exc)

                    logger.info("operator_suspended operator=%s invoice=%s", op.slug, invoice.invoice_number)
                except Exception as exc:
                    logger.error("suspension_error invoice=%s error=%s", invoice.invoice_number, exc)

            await db.commit()
            logger.info("billing_enforcement_job_completed")
        except Exception as exc:
            logger.error("billing_enforcement_job_error error=%s", exc)
            await db.rollback()
