"""Hashed one-time codes with a database-enforced attempt limit.

The Redis rate limiter fails open, so it cannot be what stops a 6-digit code
from being guessed. Every wrong guess is counted on the code row itself (under
a row lock), and a code that has used its attempts is dead regardless of Redis.
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AdminOtpCode
from src.modules.admin_accounts.notifications import OTP_TTL_MINUTES
from src.utils.auth import hash_password, verify_password

OTP_MAX_ATTEMPTS = 5


def generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


async def issue_code(db: AsyncSession, *, admin_user_id: uuid.UUID, purpose: str, phone: str) -> tuple[AdminOtpCode, str]:
    """Create a fresh code (superseding any open one for the same purpose).
    Adds and flushes; the caller commits."""
    now = datetime.now(timezone.utc)
    await db.execute(
        update(AdminOtpCode)
        .where(
            AdminOtpCode.admin_user_id == admin_user_id,
            AdminOtpCode.purpose == purpose,
            AdminOtpCode.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    code = generate_code()
    row = AdminOtpCode(
        admin_user_id=admin_user_id,
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
    row: AdminOtpCode | None = None
    reason: str | None = None  # "no_code" | "expired" | "locked" | "mismatch"


async def verify_code(db: AsyncSession, *, admin_user_id: uuid.UUID, purpose: str, code: str) -> VerifyResult:
    """Check ``code`` against the admin's open code for ``purpose``.

    Commits the attempt counter on a miss (so a later rollback can't undo it)
    and marks the row consumed on a hit (the caller commits that together with
    whatever the successful verification unlocks).
    """
    now = datetime.now(timezone.utc)
    row = (
        await db.execute(
            select(AdminOtpCode)
            .where(
                AdminOtpCode.admin_user_id == admin_user_id,
                AdminOtpCode.purpose == purpose,
                AdminOtpCode.consumed_at.is_(None),
            )
            .order_by(AdminOtpCode.created_at.desc())
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
