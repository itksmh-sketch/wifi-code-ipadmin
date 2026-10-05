"""Job: send_voucher_sms — one per successful payment, enqueued by
``PaymentService.resolve_successful_payment`` after the payment commits.

Exactly-once, in three layers:
  1. Only the caller that flips the payment to success (row locked FOR UPDATE)
     enqueues, so a portal poll racing a webhook produces one enqueue.
  2. The job id is ``voucher_sms:<payment id>``; arq refuses a second enqueue
     while that job is queued, running, or its result is kept (1 h).
  3. Registered with ``max_tries=1``: arq never re-runs it, so a worker dying
     after the provider accepted the message can't text the buyer again. The
     cost is that a send lost that way is not retried; it is logged instead.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select

from src.db.base import async_session_factory
from src.db.models import PaymentTransaction, Plan, Voucher
from src.jobs.queue import get_redis_pool
from src.modules.payments.types import PaymentStatus
from src.modules.sms.voucher_delivery import deliver_voucher_sms

logger = structlog.get_logger(__name__)

JOB_NAME = "send_voucher_sms"


def job_id_for(payment_tx_id: uuid.UUID | str) -> str:
    return f"voucher_sms:{payment_tx_id}"


async def enqueue_voucher_sms(payment_tx_id: uuid.UUID) -> bool:
    """Queue the SMS for a paid transaction. Returns False when it could not be
    queued (logged at ERROR); the caller carries on — the voucher is already
    on the success page."""
    redis = None
    try:
        redis = await get_redis_pool()
        job = await redis.enqueue_job(JOB_NAME, str(payment_tx_id), _job_id=job_id_for(payment_tx_id))
        if job is None:
            logger.info("voucher_sms_enqueue_duplicate", payment_tx_id=str(payment_tx_id))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("voucher_sms_enqueue_failed", payment_tx_id=str(payment_tx_id), error=str(exc))
        return False
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:  # noqa: BLE001
                pass


async def send_voucher_sms(ctx, payment_tx_id: str) -> str:
    enqueued = (ctx or {}).get("enqueue_time")
    queued_ms = None
    if isinstance(enqueued, datetime):
        queued_ms = int((datetime.now(timezone.utc) - enqueued).total_seconds() * 1000)
    logger.info(
        "voucher_sms_job_started",
        payment_tx_id=payment_tx_id,
        job_try=(ctx or {}).get("job_try"),
        queued_ms=queued_ms,
    )

    async with async_session_factory() as db:
        tx = await db.get(PaymentTransaction, uuid.UUID(payment_tx_id))
        if tx is None or tx.status != PaymentStatus.SUCCESS.value or not tx.voucher_id or not tx.phone_number:
            logger.warning(
                "voucher_sms_job_skipped",
                payment_tx_id=payment_tx_id,
                reason="not_a_paid_transaction_with_voucher_and_phone",
            )
            return "skipped_not_eligible"
        voucher = await db.get(Voucher, tx.voucher_id)
        plan = (
            await db.execute(
                select(Plan).where(Plan.id == tx.plan_id, Plan.isp_operator_id == tx.isp_operator_id)
            )
        ).scalar_one_or_none()
        if voucher is None or plan is None:
            logger.error("voucher_sms_job_skipped", payment_tx_id=payment_tx_id, reason="voucher_or_plan_missing")
            return "skipped_not_eligible"
        return await deliver_voucher_sms(
            db, operator_id=tx.isp_operator_id, to=tx.phone_number, code=voucher.code, plan=plan,
            payment_transaction_id=tx.id,
        )
