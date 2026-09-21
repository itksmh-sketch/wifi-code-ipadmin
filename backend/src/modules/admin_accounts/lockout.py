"""Account lockouts for operator admins: password login, and PIN entry.

Two independent mechanisms, deliberately not sharing state — different
thresholds, and either one tripping must not consume the other's budget:

    password login   5 consecutive failures  -> locked 3h
    PIN entry       10 consecutive failures  -> locked 3h, and every session
                                                is killed (token_version bump)

Why the counters live in Postgres rather than Redis: middleware.rate_limit
fails open on any Redis fault by design, which is fine for burst control but
useless as the thing that actually stops guessing. otp.py makes the same
argument for OTP attempts. The IP limiter stays in front of both as a cheap
outer layer.

A lock is a timestamp, not a flag, so it lapses on its own with no sweeper job.
The attempt counter is left at the threshold while the lock holds (it reads as
"why is this locked") and is reset on the first attempt after the lock lapses,
or immediately on any success.

Notification: the admin's verified phone gets one SMS per lock, never per
attempt. Suppression falls out of the state machine rather than needing a
separate "last notified" column — an attempt made while a lock is live is
rejected before it can increment, so the unlocked -> locked transition that
triggers the send happens exactly once per lock period.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import async_session_factory
from src.db.models import AdminSecurityEvent, AdminUser

logger = logging.getLogger("admin_accounts.lockout")

LOGIN_MAX_ATTEMPTS = 5
PIN_MAX_ATTEMPTS = 10
LOCKOUT_HOURS = 3

LOGIN = "login"
PIN = "pin"

# kind -> (attempt column, lock column, threshold, event_type)
_KINDS: dict[str, tuple[str, str, int, str]] = {
    LOGIN: ("login_attempt_count", "login_locked_until", LOGIN_MAX_ATTEMPTS, "login_lockout"),
    PIN: ("pin_attempt_count", "pin_locked_until", PIN_MAX_ATTEMPTS, "pin_lockout"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def locked_until(admin: AdminUser, kind: str) -> datetime | None:
    """The live lock expiry for ``kind``, or None if not currently locked.

    A lapsed timestamp reads as unlocked here; it is cleared lazily by the next
    register_failure so a read path never has to write.
    """
    _, lock_attr, _, _ = _KINDS[kind]
    until = getattr(admin, lock_attr)
    return until if until is not None and until > _now() else None


def is_locked(admin: AdminUser, kind: str) -> bool:
    return locked_until(admin, kind) is not None


def clear(admin: AdminUser, kind: str) -> None:
    """Zero one mechanism's counter and lock. Caller commits."""
    count_attr, lock_attr, _, _ = _KINDS[kind]
    setattr(admin, count_attr, 0)
    setattr(admin, lock_attr, None)


def clear_all(admin: AdminUser) -> None:
    """Zero both mechanisms. Called from every path that proves account
    ownership by setting a new password — self-service change, reset-token
    redemption, and a platform-owner reset — alongside the existing
    security_answer_attempt_count clear in _apply_new_password.

    Rationale: someone who can set the password is the owner, so holding them
    out of their own account for the rest of a 3h window is pure downside. It
    also gives support a documented un-stick path for a locked-out operator.
    """
    clear(admin, LOGIN)
    clear(admin, PIN)


async def register_failure(
    db: AsyncSession,
    admin: AdminUser,
    kind: str,
    *,
    client_ip: str | None = None,
) -> uuid.UUID | None:
    """Count one failed attempt and lock if that hits the threshold.

    Commits (like otp.verify_code does) so the count survives a later rollback
    of whatever the caller was doing. Returns the AdminSecurityEvent id when
    this attempt is what caused the lock — the caller hands that to
    send_lockout_notification as a background task — or None otherwise.
    """
    count_attr, lock_attr, threshold, event_type = _KINDS[kind]
    now = _now()

    previous = getattr(admin, lock_attr)
    if previous is not None and previous <= now:
        # The previous lock has lapsed: this failure starts a fresh run.
        setattr(admin, lock_attr, None)
        setattr(admin, count_attr, 0)

    count = int(getattr(admin, count_attr) or 0) + 1
    setattr(admin, count_attr, count)

    if count < threshold:
        await db.commit()
        return None

    setattr(admin, lock_attr, now + timedelta(hours=LOCKOUT_HOURS))
    if kind == PIN:
        # "Log the user out" on a PIN lockout: token_version is embedded in
        # every admin JWT and checked on every request and refresh, so this
        # kills all access and refresh tokens on the next call. Not done for a
        # login lockout — there is no session to kill, and bumping it there
        # would sign out an admin's healthy sessions on another device because
        # someone else guessed at their password.
        admin.token_version = int(admin.token_version or 0) + 1

    event = AdminSecurityEvent(
        admin_user_id=admin.id,
        isp_operator_id=admin.isp_operator_id,
        event_type=event_type,
        detail={"attempts": count, "client_ip": client_ip, "locked_for_hours": LOCKOUT_HOURS},
    )
    db.add(event)
    await db.commit()
    logger.warning(
        "admin_%s admin_id=%s attempts=%s locked_until=%s", event_type, admin.id, count, getattr(admin, lock_attr)
    )
    return event.id


async def send_lockout_notification(admin_id: uuid.UUID, kind: str, event_id: uuid.UUID) -> None:
    """Background task: SMS the admin's verified phone and record the outcome.

    Runs in its own session — the request that triggered it has already
    committed the lock and returned. A failure here is logged and recorded on
    the event row; it never un-locks the account.
    """
    from src.modules.admin_accounts.notifications import send_lockout_sms

    try:
        async with async_session_factory() as db:
            admin = (await db.execute(select(AdminUser).where(AdminUser.id == admin_id))).scalar_one_or_none()
            event = (
                await db.execute(select(AdminSecurityEvent).where(AdminSecurityEvent.id == event_id))
            ).scalar_one_or_none()
            if admin is None:
                return
            if not (admin.phone_verified and admin.phone):
                # Nothing to send to. Email is disabled platform-wide, so there
                # is no fallback channel — record why and move on.
                if event is not None:
                    event.sms_error = "no_verified_phone"
                    await db.commit()
                logger.warning("admin_lockout_sms_skipped admin_id=%s kind=%s reason=no_verified_phone", admin_id, kind)
                return

            result = await send_lockout_sms(admin, kind=kind)
            if event is not None:
                event.sms_sent = bool(result.success)
                event.sms_error = None if result.success else (result.error or "unknown")
                await db.commit()
    except Exception as exc:  # a notification must never surface as a request error
        logger.error("admin_lockout_sms_failed admin_id=%s kind=%s error=%s", admin_id, kind, exc)
