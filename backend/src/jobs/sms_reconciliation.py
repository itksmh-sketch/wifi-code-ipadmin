"""Job: sms_reconciliation — runs daily.

Two independent, log-and-alert-only checks over the platform-provided SMS
gateway. Neither ever writes to sms_usage_records, OperatorInvoiceLineItem, or
any invoice — financial records only ever change through the metering
(sms.metering) and rollup (sms.billing_rollup) pipeline. Even if this job
wanted to auto-correct something, the balance-delta signal below is
platform-wide, not per-operator, so there is no invoice it could safely
attribute a correction to — log-and-alert is not just the cautious choice
here, it's the only coherent one.

1. Balance-delta drift: compares Arkesel's own sms_balance draw-down since the
   last run against SUM(segment_count) recorded locally over the same window.
   A one-off top-up on Arkesel's own dashboard (outside this app) pushes the
   comparison the OTHER way (recorded >= consumed) and is not alertable —
   only "Arkesel consumed more than we recorded" (a plausible missed
   sms_usage_records write) is. The per-message cost-verification loop this
   could theoretically be extended into is a deliberately deferred follow-up,
   not built here — this coarse check ships first; the expensive, rate-limit
   sensitive precise version is only worth building if this one ever fires.

2. Unbilled-usage-on-dead-accounts: a separate, purely-local query for
   sms_usage_records rows still unclaimed (invoice_line_item_id IS NULL)
   after ~35 days — past one ordinary billing cycle, so it doesn't fire on
   usage simply waiting for next month's arrears rollup. Informational only:
   a permanently-suspended operator's pre-suspension usage staying unclaimed
   forever is a deliberate, accepted outcome (see sms.billing_rollup's
   module docstring), not a fault.

Runs once daily — this job is purely retrospective monitoring with no
load-bearing role in the send/metering/billing path, so there's no
correctness reason to run it more often. Scheduled at an off-the-round-number
minute (see jobs/worker.py) to avoid clustering with the many jobs already
registered on :00/:05/:10-style marks.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import async_session_factory
from src.db.models import SMSUsageRecord
from src.modules.credentials.service import load_credentials
from src.modules.platform import platform_sms_credentials_service as platform_creds
from src.modules.platform.settings_service import get_setting, set_setting
from src.modules.sms.providers.arkesel import ArkeselSMSProvider

logger = structlog.get_logger(__name__)

_LAST_BALANCE_KEY = "sms_reconciliation_last_balance"
_LAST_CHECKED_AT_KEY = "sms_reconciliation_last_checked_at"
# Ordinary timing skew between when Arkesel debits and when this job's
# snapshot lands — not itself evidence of a missing usage record.
_DRIFT_TOLERANCE_SEGMENTS = Decimal("2")
_STALE_UNBILLED_THRESHOLD = timedelta(days=35)

__all__ = ["reconcile_sms_usage"]


async def _notifications_share_gateway_account(db: AsyncSession) -> bool:
    """Whether operator-notification SMS currently draws on the same Arkesel
    account as the metered gateway.

    Stated explicitly by the admin rather than detected: two API keys from one
    account are indistinguishable from here (no account identifier is exposed),
    and inferring it by comparing balances races against any send landing
    between the two reads. False when nothing is configured — no notification
    credential means no notification spend to confuse the drift check with.
    """
    from src.modules.credentials.service import load_credentials
    from src.modules.platform import notification_sms_credentials_service as notif_creds

    credential = await notif_creds.get_active_credential(db)
    if credential is None:
        return False
    return bool(load_credentials(credential).get("shares_gateway_account", True))


async def _check_balance_drift(db: AsyncSession, *, now: datetime) -> None:
    credential = await platform_creds.get_active_credential(db)
    if credential is None:
        logger.info("sms_reconciliation_skipped", reason="no_active_platform_credential")
        return

    values = load_credentials(credential)
    provider = ArkeselSMSProvider(api_key=values["api_key"], sender_id=values.get("sender_id", ""))
    try:
        current_balance = await provider.get_sms_balance()
    except Exception as exc:  # noqa: BLE001 — a fetch failure is logged, not fatal to the job
        logger.error("sms_reconciliation_balance_fetch_failed", error=str(exc))
        return
    if current_balance is None:
        logger.error("sms_reconciliation_balance_unavailable")
        return

    last_balance_raw = await get_setting(db, _LAST_BALANCE_KEY)
    last_checked_raw = await get_setting(db, _LAST_CHECKED_AT_KEY)

    if not last_balance_raw or not last_checked_raw:
        logger.info("sms_reconciliation_baseline_established", balance=str(current_balance))
    else:
        try:
            previous_balance = Decimal(last_balance_raw)
            last_checked_at = datetime.fromisoformat(last_checked_raw)
        except (InvalidOperation, ValueError):
            logger.error(
                "sms_reconciliation_checkpoint_unparseable",
                last_balance_raw=last_balance_raw, last_checked_raw=last_checked_raw,
            )
            previous_balance = None
            last_checked_at = None

        if previous_balance is not None and last_checked_at is not None:
            consumed = previous_balance - current_balance
            # KNOWN LIMITATION while the notification credential
            # (platform_notification_sms_credentials) is a second API key on the
            # SAME Arkesel account as the gateway rather than a separate account:
            # `consumed` reflects the whole account's balance movement, so it
            # includes operator-notification sends, while `recorded` below counts
            # only metered gateway sends (notifications write no usage record).
            # Every notification therefore widens drift in the "consumed >
            # recorded" direction — the same signature as a genuinely missed
            # metering write. Drift alerts are NOT trustworthy until the two are
            # on separate Arkesel accounts; treat them as expected noise
            # proportional to notification volume, not as evidence of a bug.
            #
            # Deliberately NOT filtered by is_diagnostic, unlike the stale
            # report below and roll_up_sms_usage. A test send still burned real
            # Arkesel credits, so it still has to count here — this check
            # compares physical balance movement against what was recorded, not
            # what was billable. Filtering diagnostic rows out would subtract
            # them from `recorded` while Arkesel's balance still reflects them,
            # manufacturing drift that looks exactly like a missed write.
            recorded = (
                await db.execute(
                    select(func.coalesce(func.sum(SMSUsageRecord.segment_count), 0)).where(
                        SMSUsageRecord.sent_at >= last_checked_at,
                        SMSUsageRecord.sent_at < now,
                    )
                )
            ).scalar() or 0
            recorded = Decimal(recorded)
            drift = consumed - recorded

            if drift > _DRIFT_TOLERANCE_SEGMENTS:
                # While notifications share the gateway's Arkesel account, drift
                # in this direction is expected and proportional to notification
                # volume, so it is reported without alerting — a daily ERROR
                # nobody can act on is how a real alert gets ignored. Untick
                # "shares the gateway's Arkesel account" on the settings card
                # once a separate account exists and this returns to ERROR on
                # the next run, with no code change.
                if await _notifications_share_gateway_account(db):
                    logger.info(
                        "sms_reconciliation_drift_expected_shared_account",
                        consumed=str(consumed), recorded=str(recorded), drift=str(drift),
                        window_start=last_checked_at.isoformat(), window_end=now.isoformat(),
                        note="notification SMS shares the gateway Arkesel account; drift is not isolated",
                    )
                else:
                    logger.error(
                        "sms_reconciliation_drift_detected",
                        consumed=str(consumed), recorded=str(recorded), drift=str(drift),
                        window_start=last_checked_at.isoformat(), window_end=now.isoformat(),
                    )
            else:
                logger.info(
                    "sms_reconciliation_ok",
                    consumed=str(consumed), recorded=str(recorded), drift=str(drift),
                )

    await set_setting(db, _LAST_BALANCE_KEY, str(current_balance))
    await set_setting(db, _LAST_CHECKED_AT_KEY, now.isoformat())


async def _check_stale_unbilled_usage(db: AsyncSession, *, now: datetime) -> None:
    cutoff = now - _STALE_UNBILLED_THRESHOLD
    rows = (
        await db.execute(
            select(
                SMSUsageRecord.isp_operator_id,
                func.count(SMSUsageRecord.id),
                func.sum(SMSUsageRecord.amount_ghs),
                func.min(SMSUsageRecord.sent_at),
            )
            .where(
                SMSUsageRecord.invoice_line_item_id.is_(None),
                # Diagnostic rows are unclaimed permanently and on purpose —
                # roll_up_sms_usage skips them by design. Including them here
                # would warn about the same rows every day forever, which is
                # exactly the kind of standing false alarm that trains someone
                # to ignore this report.
                SMSUsageRecord.is_diagnostic.is_(False),
            )
            .group_by(SMSUsageRecord.isp_operator_id)
            .having(func.min(SMSUsageRecord.sent_at) < cutoff)
        )
    ).all()
    for operator_id, record_count, amount_ghs, oldest_sent_at in rows:
        logger.warning(
            "sms_reconciliation_stale_unbilled_usage",
            operator=str(operator_id), record_count=record_count,
            amount_ghs=str(amount_ghs), oldest_sent_at=oldest_sent_at.isoformat(),
        )


async def reconcile_sms_usage(ctx=None):
    """Daily job. Log-and-alert only — see module docstring."""
    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        try:
            await _check_balance_drift(db, now=now)
            await _check_stale_unbilled_usage(db, now=now)
            await db.commit()
        except Exception as exc:
            logger.error("sms_reconciliation_job_error", error=str(exc))
            await db.rollback()
