"""Voucher-code SMS to the buyer after a successful payment.

Runs in the worker (``src.jobs.voucher_sms``), never in the request that
confirmed the payment: the provider call can take up to its 10 s timeout, and
it used to block the captive portal's payment-status poll for that long.

Never raises. The voucher is already issued and shown on the success page; an
SMS problem is logged, not propagated, so arq has nothing to retry — a retry
after a send that went out would text the buyer twice.
"""
from __future__ import annotations

import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ISPOperator, Plan
from src.modules.platform.platform_sms_rate import (
    PlatformSMSRateNotConfigured,
    get_current_platform_sms_rate,
)
from src.modules.sms.metering import record_platform_sms_usage
from src.modules.sms.provider_resolver import (
    PLATFORM_GATEWAY_PROVIDER_KEY,
    resolve_active_sms_provider,
)
from src.modules.sms.providers.registry import build_sms_provider
from src.modules.sms.segmentation import count_sms_segments
from src.modules.sms.service import build_voucher_sms_message

__all__ = ["deliver_voucher_sms"]

logger = logging.getLogger("sms.voucher_delivery")


async def deliver_voucher_sms(
    db: AsyncSession, *, operator_id: uuid.UUID, to: str, code: str, plan: Plan
) -> str:
    """Send one voucher SMS. Returns the outcome (``sent``, ``failed``,
    ``skipped_no_provider``, ``skipped_no_rate``, ``exception``) for the job
    result and tests."""
    resolved = await resolve_active_sms_provider(db, operator_id)
    if resolved is None:
        # No SMS gateway configured for this operator — deliver the voucher on
        # the success page only (matches the old sms_provider='' no-op).
        logger.info(
            "voucher_sms_skipped operator=%s reason=no_active_sms_provider", operator_id,
        )
        return "skipped_no_provider"

    provider_key, credentials = resolved
    # Resolved BEFORE sending, not after: a send we can't price must not happen
    # at all — never bill for a send we can't confirm, and never send one we
    # can't bill for.
    platform_rate = None
    if provider_key == PLATFORM_GATEWAY_PROVIDER_KEY:
        try:
            platform_rate = await get_current_platform_sms_rate(db)
        except PlatformSMSRateNotConfigured:
            logger.error(
                "platform_sms_rate_not_configured operator=%s to=%s — send skipped",
                operator_id, to,
            )
            return "skipped_no_rate"

    try:
        # The operator's own template is a nicety; failing to read it must
        # never cost the buyer their voucher SMS, so this degrades to the
        # platform default rather than skipping.
        operator = None
        try:
            operator = await db.get(ISPOperator, operator_id)
        except Exception as exc:
            logger.warning(
                "voucher_sms_template_lookup_failed operator=%s error=%s", operator_id, exc,
            )
        message = build_voucher_sms_message(
            code=code,
            plan=plan,
            template=getattr(operator, "voucher_sms_template", None),
            operator_name=getattr(operator, "name", "") or "",
        )
        result = await build_sms_provider(provider_key, credentials).send(to=to, message=message)
        if result is not None and not result.success:
            logger.error(
                "voucher_sms_send_failed operator=%s provider=%s to=%s error=%s",
                operator_id, provider_key, to, result.error,
            )
            return "failed"

        logger.info(
            "voucher_sms_sent operator=%s provider=%s to=%s", operator_id, provider_key, to,
        )
        if provider_key == PLATFORM_GATEWAY_PROVIDER_KEY:
            segment_info = count_sms_segments(message)
            amount = (Decimal(segment_info.segment_count) * platform_rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            metered = await record_platform_sms_usage(
                isp_operator_id=operator_id,
                provider_reference=result.provider_reference if result else None,
                segment_count=segment_info.segment_count,
                rate_ghs_per_segment=platform_rate,
                amount_ghs=amount,
            )
            if not metered:
                logger.error(
                    "platform_sms_usage_unrecoverable_after_retries operator=%s to=%s",
                    operator_id, to,
                )
        return "sent"
    except Exception:
        logger.exception(
            "voucher_sms_exception operator=%s provider=%s to=%s", operator_id, provider_key, to,
        )
        return "exception"
