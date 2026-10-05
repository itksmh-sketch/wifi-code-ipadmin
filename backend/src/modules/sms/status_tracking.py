"""Per-message SMS delivery status (sms_messages, migration 054).

Every send goes through ``tracked_send``. Once a provider accepts a message,
a row records who sent it (credential fingerprint, sender ID, masked number)
and when. For Arkesel the send also asks for a delivery callback, at a
per-message unguessable URL.

Status is only ever read back from Arkesel's own API (``refresh_status``):
  * a callback is a trigger to read, never trusted itself (Arkesel doesn't
    sign callbacks; a forged one can at most cause one extra lookup);
  * a 5-minute poll covers callbacks that never came, and gives up 24 hours
    after the send.

Arkesel's record has a status and ``sent_at_time`` (handed to the network),
but no delivered-at time, so ``final_status_at`` is when we first saw a final
status: within seconds when a callback triggered the read, within the poll
interval otherwise. A dispatch delay over 5 minutes is logged as a warning.

Nothing here may break a send: recording and refreshing log their failures.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import async_session_factory
from src.db.models import SMSMessage
from src.modules.sms.providers.arkesel import ArkeselSMSProvider
from src.modules.sms.providers.base import SMSProvider, key_fingerprint, mask_msisdn
from src.modules.sms.types import SMSSendResult

logger = logging.getLogger("sms.status_tracking")

CALLBACK_PATH = "/api/v1/webhooks/arkesel/sms-status"
# Providers whose status we can read back (GET /api/v2/sms/{id}).
TRACKED_PROVIDERS = frozenset({"arkesel", "arkesel_platform"})
# Arkesel statuses after which nothing changes. Anything else (SUBMITTED,
# QUEUED, unknown) keeps being polled until the window closes.
FINAL_STATUSES = frozenset({"DELIVERED", "NOT_DELIVERED", "UNDELIVERED", "EXPIRED", "PROHIBITED", "REJECTED", "FAILED"})
POLL_INTERVAL = timedelta(minutes=5)
POLL_WINDOW = timedelta(hours=24)
DISPATCH_DELAY_WARN = timedelta(minutes=5)

SCOPE_PLATFORM_GATEWAY = "platform_gateway"
SCOPE_PLATFORM_NOTIFICATION = "platform_notification"
SCOPE_OPERATOR = "operator"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, stored_hash: str | None) -> bool:
    return bool(stored_hash) and hmac.compare_digest(token_hash(token), stored_hash)


def _credential_secret(provider: SMSProvider) -> str | None:
    # Arkesel / Africa's Talking: api_key; Hubtel: client_secret.
    return getattr(provider, "api_key", None) or getattr(provider, "client_secret", None)


async def _callback_url() -> tuple[str | None, str | None]:
    """(token, url), or (None, None) when the public base isn't HTTPS: Arkesel
    would be calling a plain-http URL, so rely on the poll instead."""
    from src.modules.webhooks.urls import webhook_base_url

    try:
        base = await webhook_base_url()
    except Exception as exc:  # noqa: BLE001
        logger.warning("sms_callback_base_lookup_failed error=%s", exc)
        return None, None
    if not base.startswith("https://"):
        return None, None
    token = secrets.token_urlsafe(24)
    return token, f"{base}{CALLBACK_PATH}/{token}"


async def tracked_send(
    provider: SMSProvider,
    *,
    to: str,
    message: str,
    purpose: str,
    provider_key: str,
    credential_scope: str,
    operator_id: uuid.UUID | None = None,
    payment_transaction_id: uuid.UUID | None = None,
) -> SMSSendResult:
    """``provider.send`` plus a sms_messages row once the provider accepts it."""
    token = None
    if isinstance(provider, ArkeselSMSProvider):
        token, url = await _callback_url()
        result = await provider.send(to=to, message=message, callback_url=url)
    else:
        result = await provider.send(to=to, message=message)
    if result is not None and result.success:
        await record_accepted(
            provider_key=provider_key,
            credential_scope=credential_scope,
            credential_fp=key_fingerprint(_credential_secret(provider)),
            sender_id=getattr(provider, "sender_id", "") or "",
            to=to,
            purpose=purpose,
            provider_reference=result.provider_reference,
            callback_token=token,
            operator_id=operator_id,
            payment_transaction_id=payment_transaction_id,
        )
    return result


async def record_accepted(
    *,
    provider_key: str,
    credential_scope: str,
    credential_fp: str,
    sender_id: str,
    to: str,
    purpose: str,
    provider_reference: str | None,
    callback_token: str | None,
    operator_id: uuid.UUID | None,
    payment_transaction_id: uuid.UUID | None,
) -> bool:
    """Own session (callers hold none, or one mid-transaction); never raises."""
    stmt = pg_insert(SMSMessage).values(
        isp_operator_id=operator_id,
        payment_transaction_id=payment_transaction_id,
        purpose=purpose[:32],
        provider=provider_key[:32],
        credential_scope=credential_scope,
        credential_fp=credential_fp,
        sender_id=sender_id[:32],
        recipient_masked=mask_msisdn(to),
        provider_reference=provider_reference,
        callback_token_hash=token_hash(callback_token) if callback_token else None,
    )
    if provider_reference is not None:
        stmt = stmt.on_conflict_do_nothing(constraint="uq_sms_messages_provider_reference")
    try:
        async with async_session_factory() as db:
            await db.execute(stmt)
            await db.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "sms_message_record_failed provider=%s provider_reference=%s purpose=%s error=%s",
            provider_key, provider_reference, purpose, exc,
        )
        return False


async def _status_credentials(db: AsyncSession, row: SMSMessage) -> dict | None:
    if row.credential_scope == SCOPE_PLATFORM_GATEWAY:
        from src.modules.credentials.service import load_credentials
        from src.modules.platform import platform_sms_credentials_service as platform_creds

        credential = await platform_creds.get_active_credential(db)
        return load_credentials(credential) if credential is not None else None
    if row.credential_scope == SCOPE_PLATFORM_NOTIFICATION:
        from src.modules.platform.notification_sms_credentials_service import resolve_notification_sms

        resolved = await resolve_notification_sms(db)
        return resolved[1] if resolved else None
    if row.credential_scope == SCOPE_OPERATOR and row.isp_operator_id:
        from src.modules.sms.provider_resolver import resolve_active_sms_provider

        resolved = await resolve_active_sms_provider(db, row.isp_operator_id)
        return resolved[1] if resolved and resolved[0] == row.provider else None
    return None


async def refresh_status(db: AsyncSession, row: SMSMessage, *, source: str) -> str:
    """Read one message's status from Arkesel and store it. Commits. Returns
    the outcome for job results and tests."""
    now = datetime.now(timezone.utc)
    if row.provider not in TRACKED_PROVIDERS or not row.provider_reference:
        return "untracked"
    row.last_checked_at = now
    row.check_count = (row.check_count or 0) + 1
    try:
        creds = await _status_credentials(db, row)
        if not creds or not creds.get("api_key"):
            await db.commit()
            logger.warning("sms_status_no_credentials id=%s scope=%s", row.id, row.credential_scope)
            return "no_credentials"
        if key_fingerprint(creds["api_key"]) != row.credential_fp:
            # The key was rotated since: Arkesel won't show this message to
            # the new key (or it's a different account). Don't guess.
            await db.commit()
            logger.warning(
                "sms_status_credential_changed id=%s sent_with=%s current=%s",
                row.id, row.credential_fp, key_fingerprint(creds["api_key"]),
            )
            return "credential_changed"
        st = await ArkeselSMSProvider(api_key=creds["api_key"], sender_id=creds.get("sender_id", "")).get_message_status(
            row.provider_reference
        )
    except Exception as exc:  # noqa: BLE001
        await db.commit()
        logger.warning("sms_status_lookup_failed id=%s ref=%s error=%s", row.id, row.provider_reference, exc)
        return "lookup_failed"

    first_sent_at = st.sent_at is not None and row.provider_sent_at is None
    row.provider_status = st.status
    if st.sent_at is not None:
        row.provider_sent_at = st.sent_at
    outcome = "pending"
    if st.status in FINAL_STATUSES and row.final_status_at is None:
        row.final_status_at = now
        row.status_source = source
        outcome = "final"
    await db.commit()

    accepted = row.accepted_at
    if first_sent_at and accepted is not None and st.sent_at - accepted > DISPATCH_DELAY_WARN:
        logger.warning(
            "sms_dispatch_delayed id=%s ref=%s purpose=%s to=%s accepted_at=%s provider_sent_at=%s delay_s=%d",
            row.id, row.provider_reference, row.purpose, row.recipient_masked,
            accepted.isoformat(), st.sent_at.isoformat(), int((st.sent_at - accepted).total_seconds()),
        )
    logger.info(
        "sms_status id=%s ref=%s status=%s source=%s sent_at=%s",
        row.id, row.provider_reference, st.status, source, st.sent_at.isoformat() if st.sent_at else None,
    )
    return outcome


async def pending_for_poll(db: AsyncSession, *, now: datetime, limit: int = 200) -> list[SMSMessage]:
    """Messages still without a final status, sent in the last 24 hours, not
    checked in the last poll interval (a callback-triggered read counts)."""
    recheck_before = now - POLL_INTERVAL + timedelta(seconds=30)
    return list(
        (
            await db.execute(
                select(SMSMessage)
                .where(
                    SMSMessage.final_status_at.is_(None),
                    SMSMessage.accepted_at > now - POLL_WINDOW,
                    SMSMessage.provider.in_(TRACKED_PROVIDERS),
                    SMSMessage.provider_reference.is_not(None),
                    (SMSMessage.last_checked_at.is_(None)) | (SMSMessage.last_checked_at < recheck_before),
                )
                .order_by(SMSMessage.accepted_at)
                .limit(limit)
            )
        ).scalars().all()
    )
