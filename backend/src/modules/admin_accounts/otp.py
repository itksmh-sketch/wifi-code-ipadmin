"""Hashed one-time codes with a database-enforced attempt limit.

The Redis rate limiter fails open, so it cannot be what stops a 6-digit code
from being guessed. Every wrong guess is counted on the code row itself (under
a row lock), and a code that has used its attempts is dead regardless of Redis.

Two stores share this one code path: operator admins (admin_otp_codes) and the
platform owner (platform_owner_otp_codes). The caller names the account with
exactly one of ``admin_user_id=`` / ``platform_owner_id=``, and that keyword
picks the table. There is deliberately no separate "model" argument: with a
model and an id passed independently, a platform-owner id checked against the
admin table would not fail loudly, it would just find no code. Tying the table
to the keyword makes that mismatch impossible to write.
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AdminOtpCode, PlatformOwnerOtpCode
from src.modules.admin_accounts.notifications import OTP_TTL_MINUTES
from src.utils.auth import hash_password, verify_password

OTP_MAX_ATTEMPTS = 5

OtpCode = AdminOtpCode | PlatformOwnerOtpCode


@dataclass(frozen=True)
class _Store:
    model: type
    owner_column: str
    # Checked here as well as by the database (enum / CHECK), so a typo fails
    # before the supersede UPDATE runs, with a message that names the store.
    purposes: frozenset[str]


_ADMIN = _Store(AdminOtpCode, "admin_user_id", frozenset({"onboarding", "reset", "phone_change", "pin_reset"}))
_PLATFORM_OWNER = _Store(
    PlatformOwnerOtpCode, "platform_owner_id", frozenset({"setup", "reset", "challenge_reset", "phone_change"})
)


def _resolve(admin_user_id: uuid.UUID | None, platform_owner_id: uuid.UUID | None, purpose: str) -> tuple[_Store, uuid.UUID]:
    if (admin_user_id is None) == (platform_owner_id is None):
        raise TypeError("pass exactly one of admin_user_id= or platform_owner_id=")
    store, owner_id = (_ADMIN, admin_user_id) if admin_user_id is not None else (_PLATFORM_OWNER, platform_owner_id)
    if purpose not in store.purposes:
        raise ValueError(f"{purpose!r} is not an OTP purpose for {store.model.__tablename__}")
    return store, owner_id


def generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


async def issue_code(
    db: AsyncSession,
    *,
    purpose: str,
    phone: str,
    admin_user_id: uuid.UUID | None = None,
    platform_owner_id: uuid.UUID | None = None,
) -> tuple[OtpCode, str]:
    """Create a fresh code (superseding any open one for the same purpose).
    Adds and flushes; the caller commits."""
    store, owner_id = _resolve(admin_user_id, platform_owner_id, purpose)
    model, owner_col = store.model, getattr(store.model, store.owner_column)
    now = datetime.now(timezone.utc)
    await db.execute(
        update(model)
        .where(
            owner_col == owner_id,
            model.purpose == purpose,
            model.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    code = generate_code()
    row = model(
        **{store.owner_column: owner_id},
        purpose=purpose,
        phone=phone,
        code_hash=hash_password(code),
        expires_at=now + timedelta(minutes=OTP_TTL_MINUTES),
        attempt_count=0,
    )
    db.add(row)
    await db.flush()
    return row, code


@dataclass
class VerifyResult:
    ok: bool
    row: OtpCode | None = None
    reason: str | None = None  # "no_code" | "expired" | "locked" | "mismatch"


async def verify_code(
    db: AsyncSession,
    *,
    purpose: str,
    code: str,
    admin_user_id: uuid.UUID | None = None,
    platform_owner_id: uuid.UUID | None = None,
) -> VerifyResult:
    """Check ``code`` against the account's open code for ``purpose``.

    Commits the attempt counter on a miss (so a later rollback can't undo it)
    and marks the row consumed on a hit (the caller commits that together with
    whatever the successful verification unlocks).
    """
    store, owner_id = _resolve(admin_user_id, platform_owner_id, purpose)
    model, owner_col = store.model, getattr(store.model, store.owner_column)
    now = datetime.now(timezone.utc)
    row = (
        await db.execute(
            select(model)
            .where(
                owner_col == owner_id,
                model.purpose == purpose,
                model.consumed_at.is_(None),
            )
            .order_by(model.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return VerifyResult(False, reason="no_code")
    if row.expires_at <= now:
        return VerifyResult(False, row, reason="expired")
    if row.attempt_count >= OTP_MAX_ATTEMPTS:
        return VerifyResult(False, row, reason="locked")

    candidate = (code or "").strip()
    if len(candidate) == 6 and candidate.isdigit() and verify_password(candidate, row.code_hash):
        row.consumed_at = now
        await db.flush()
        return VerifyResult(True, row)

    row.attempt_count = int(row.attempt_count or 0) + 1
    await db.commit()
    return VerifyResult(False, row, reason="locked" if row.attempt_count >= OTP_MAX_ATTEMPTS else "mismatch")


def failure_message(result: VerifyResult) -> str:
    if result.reason == "locked":
        return "Too many incorrect attempts. Request a new code."
    if result.reason in ("expired", "no_code"):
        return "This code has expired or is no longer valid. Request a new code."
    return "Incorrect code. Check the SMS and try again."
