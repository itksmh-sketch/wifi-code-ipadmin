"""Metering write for the platform-provided SMS gateway.

Called once, synchronously, immediately after a confirmed-successful
platform-gateway send — never from a DLR webhook or a status poll, both
unreliable for per-message billing data. By the time this runs, every value
is already computed (segment count locally, rate snapshotted at send time);
the only thing that can fail is persisting them.

Retries the write a bounded few times, each attempt on a fresh session (a
session behind a failed commit must not be reused), and is idempotent on
provider_reference so a retry racing a commit whose acknowledgment was lost
doesn't double-bill. If every retry is exhausted, the failure is logged at
ERROR with every value needed to manually reconstruct the row — recoverable,
just not automatically. See docs discussion: the send already happened and is
never rolled back or retried because of a metering failure.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.db.base import async_session_factory
from src.db.models import SMSUsageRecord

logger = logging.getLogger("sms.metering")

_MAX_ATTEMPTS = 3
_RETRY_DELAYS_SECONDS = (0.2, 0.6)

__all__ = ["record_platform_sms_usage"]


async def record_platform_sms_usage(
    *,
    isp_operator_id: uuid.UUID,
    provider_reference: str | None,
    segment_count: int,
    rate_ghs_per_segment: Decimal,
    amount_ghs: Decimal,
    provider: str = "arkesel",
) -> bool:
    """Persist one metered send. Returns True once written (or already
    present via the idempotent conflict target), False only after every retry
    is exhausted. Callers must not fail the request or retry the SMS send on
    False — the send already happened; only the record of it didn't stick."""
    stmt = pg_insert(SMSUsageRecord).values(
        isp_operator_id=isp_operator_id,
        provider=provider,
        provider_reference=provider_reference,
        segment_count=segment_count,
        rate_ghs_per_segment=rate_ghs_per_segment,
        amount_ghs=amount_ghs,
    )
    if provider_reference is not None:
        stmt = stmt.on_conflict_do_nothing(index_elements=["provider_reference"])

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with async_session_factory() as db:
                await db.execute(stmt)
                await db.commit()
            return True
        except Exception as exc:  # noqa: BLE001 — every attempt is logged or retried
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAYS_SECONDS[attempt - 1])
                continue
            logger.error(
                "sms_usage_record_write_failed operator=%s provider_reference=%s "
                "segment_count=%s rate_ghs_per_segment=%s amount_ghs=%s error=%s",
                isp_operator_id, provider_reference, segment_count,
                rate_ghs_per_segment, amount_ghs, exc,
            )
            return False
    return False  # unreachable
