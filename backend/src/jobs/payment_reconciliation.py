from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.db.base import async_session_factory
from src.db.models import PaymentTransaction
from src.modules.payments.dependencies import get_payment_service
from src.modules.payments.types import PROVIDER_UNREACHABLE_STATE, PaymentStatus

import structlog

logger = structlog.get_logger(__name__)


async def run_payment_reconciliation(ctx=None) -> dict:
    service = get_payment_service()
    now = datetime.now(timezone.utc)
    fifteen_minutes_ago = now - timedelta(minutes=15)
    two_hours_ago = now - timedelta(hours=2)
    stats = {
        "processed": 0,
        "success": 0,
        "failed": 0,
        "timeout": 0,
        "still_pending": 0,
        "unreachable": 0,
        "errors": 0,
    }

    async with async_session_factory() as db:
        result = await db.execute(
            select(PaymentTransaction).where(
                PaymentTransaction.status == PaymentStatus.PENDING.value,
                PaymentTransaction.initiated_at < fifteen_minutes_ago,
            )
        )
        pending_rows = list(result.scalars().all())

        for tx in pending_rows:
            stats["processed"] += 1
            if not tx.provider_reference:
                if tx.initiated_at < two_hours_ago:
                    old_status = tx.status
                    tx.status = PaymentStatus.FAILED.value
                    tx.failure_reason = "timeout"
                    tx.completed_at = now
                    service._log_status_change(tx, old_status, tx.status, "poll")
                    stats["timeout"] += 1
                else:
                    stats["still_pending"] += 1
                continue

            try:
                provider = await service.provider_for_transaction(db, tx)
                verify_result = await provider.verify(tx.provider_reference, expected_amount_ghs=tx.amount_ghs)
            except Exception as exc:
                # One unhealthy provider (or one bad credential row) must not
                # abort the batch and strand every remaining pending payment
                # until the next run. Skip this transaction; it stays pending
                # and is picked up again in 10 minutes.
                logger.warning(
                    "payment_reconciliation_verify_failed",
                    module=__name__,
                    internal_reference=tx.internal_reference,
                    error=str(exc),
                )
                stats["errors"] += 1
                continue

            if verify_result.provider_state == PROVIDER_UNREACHABLE_STATE:
                # Provider unreachable: no verdict, so don't let the two-hour
                # timeout branch below mark it failed on the strength of a
                # network error. Leave it pending for the next run.
                tx.last_status_check_at = now
                stats["unreachable"] += 1
                continue

            if verify_result.status == PaymentStatus.SUCCESS:
                await service.apply_provider_result(db, tx=tx, result=verify_result, trigger_source="poll")
                stats["success"] += 1
                continue

            if verify_result.status == PaymentStatus.FAILED:
                await service.apply_provider_result(db, tx=tx, result=verify_result, trigger_source="poll")
                stats["failed"] += 1
                continue

            tx = await service.apply_provider_result(db, tx=tx, result=verify_result, trigger_source="poll")

            if tx.initiated_at < two_hours_ago:
                old_status = tx.status
                tx.status = PaymentStatus.FAILED.value
                tx.failure_reason = "timeout"
                tx.completed_at = now
                service._log_status_change(tx, old_status, tx.status, "poll")
                stats["timeout"] += 1
            else:
                stats["still_pending"] += 1

        await db.commit()
    logger.info("payment_reconciliation_completed", module=__name__, **stats)
    return stats
