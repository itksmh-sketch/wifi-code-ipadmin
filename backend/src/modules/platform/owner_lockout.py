"""Account lockouts for the platform owner.

Same state machine as admin_accounts.lockout (read that module's docstring for
the reasoning: Postgres, not Redis, because the rate limiter fails open; a lock
is a timestamp that lapses on its own; one SMS per lock, never per attempt).
Kept separate rather than generalised because everything around the state
machine differs: the row type, the audit table (admin_security_events requires
an operator), the SMS wording and link, and the thresholds.

    password login   5 consecutive failures -> locked 1h
    character code   5 consecutive failures -> locked 3h

Why 1h and not the operator admins' 3h: the platform owner is a single account
with nobody above it. An operator admin locked out by someone guessing at their
email has the platform owner to un-stick them; the platform owner has no one,
so the lock is itself a denial-of-service lever against the one account that
runs the platform. A shorter lock keeps the guessing budget small (5 per hour)
while bounding what a griefer can do. A successful password change clears it.

The character-challenge lockout is stricter, and not because the challenge is
guessable (3 positions of a 31-symbol alphabet is ~1 in 30,000 per try). A
wrong answer can only arrive AFTER a correct password, so it is the strongest
compromise signal this account produces: someone has the password and not the
code. The lock is 3h, and the SMS says so plainly, with the source IP, so the
owner changes their password. The two counters never touch each other.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import async_session_factory
from src.db.models import PlatformOwner, PlatformOwnerSecurityEvent

logger = logging.getLogger("platform.owner_lockout")

LOGIN = "login"
CHALLENGE = "challenge"

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_HOURS = 1
CHALLENGE_MAX_ATTEMPTS = 5
CHALLENGE_LOCKOUT_HOURS = 3

# kind -> (attempt column, lock column, threshold, lock hours, event_type)
_KINDS: dict[str, tuple[str, str, int, int, str]] = {
    LOGIN: ("login_attempt_count", "login_locked_until", LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT_HOURS, "login_lockout"),
    CHALLENGE: (
        "challenge_attempt_count", "challenge_locked_until", CHALLENGE_MAX_ATTEMPTS, CHALLENGE_LOCKOUT_HOURS,
        "challenge_lockout",
    ),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def locked_until(owner: PlatformOwner, kind: str) -> datetime | None:
    """The live lock expiry for ``kind``, or None if not currently locked."""
    _, lock_attr, _, _, _ = _KINDS[kind]
    until = getattr(owner, lock_attr)
    return until if until is not None and until > _now() else None


def is_locked(owner: PlatformOwner, kind: str) -> bool:
    return locked_until(owner, kind) is not None


def clear(owner: PlatformOwner, kind: str) -> None:
    """Zero one mechanism's counter and lock. Caller commits."""
    count_attr, lock_attr, _, _, _ = _KINDS[kind]
    setattr(owner, count_attr, 0)
    setattr(owner, lock_attr, None)


async def register_failure(
    db: AsyncSession,
    owner: PlatformOwner,
    kind: str,
    *,
    client_ip: str | None = None,
) -> uuid.UUID | None:
    """Count one failed attempt and lock if that hits the threshold.

    Commits, so the count survives whatever the caller does next. Returns the
    security event id when this attempt caused the lock (hand it to
    send_lockout_notification as a background task), otherwise None.
    """
    count_attr, lock_attr, threshold, hours, event_type = _KINDS[kind]
    now = _now()

    previous = getattr(owner, lock_attr)
    if previous is not None and previous <= now:
        # The previous lock has lapsed: this failure starts a fresh run.
        setattr(owner, lock_attr, None)
        setattr(owner, count_attr, 0)

    count = int(getattr(owner, count_attr) or 0) + 1
    setattr(owner, count_attr, count)

    if count < threshold:
        await db.commit()
        return None

    setattr(owner, lock_attr, now + timedelta(hours=hours))
    event = PlatformOwnerSecurityEvent(
        platform_owner_id=owner.id,
        event_type=event_type,
        detail={"attempts": count, "client_ip": client_ip, "locked_for_hours": hours},
    )
    db.add(event)
    await db.commit()
    logger.warning(
        "platform_owner_%s owner_id=%s attempts=%s locked_until=%s", event_type, owner.id, count, getattr(owner, lock_attr)
    )
    return event.id


async def send_lockout_notification(owner_id: uuid.UUID, kind: str, event_id: uuid.UUID) -> None:
    """Background task: SMS the owner's verified phone and record the outcome
    on the event row. A failure is logged and recorded; it never unlocks."""
    from src.modules.admin_accounts.notifications import send_platform_owner_lockout_sms

    try:
        async with async_session_factory() as db:
            owner = (await db.execute(select(PlatformOwner).where(PlatformOwner.id == owner_id))).scalar_one_or_none()
            event = (
                await db.execute(select(PlatformOwnerSecurityEvent).where(PlatformOwnerSecurityEvent.id == event_id))
            ).scalar_one_or_none()
            if owner is None:
                return
            if not (owner.phone_verified and owner.phone):
                if event is not None:
                    event.sms_error = "no_verified_phone"
                    await db.commit()
                logger.warning("platform_owner_lockout_sms_skipped owner_id=%s kind=%s reason=no_verified_phone", owner_id, kind)
                return

            client_ip = (event.detail or {}).get("client_ip") if event is not None else None
            result = await send_platform_owner_lockout_sms(owner, kind=kind, client_ip=client_ip)
            if event is not None:
                event.sms_sent = bool(result.success)
                event.sms_error = None if result.success else (result.error or "unknown")
                await db.commit()
    except Exception as exc:  # a notification must never surface as a request error
        logger.error("platform_owner_lockout_sms_failed owner_id=%s kind=%s error=%s", owner_id, kind, exc)
