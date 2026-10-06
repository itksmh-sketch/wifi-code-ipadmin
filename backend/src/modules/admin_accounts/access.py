"""Who may sign in, and the platform's switches for turning that off.

Two switches, one rule:

  * per admin: ``admin_users.is_active`` (with disabled_* saying who and why);
  * per operator: ``isp_operators.admin_logins_disabled_at``, which shuts out
    every admin AND every reseller of that operator.

``admin_may_sign_in()`` / ``reseller_may_sign_in()`` are the single definition
of "allowed in". Login, refresh, the per-request guards, the reset flows and
logout all add it to the query they already run, as one more WHERE clause.
That keeps a disabled account on exactly the path an unknown one takes: same
response, same dummy-hash cost, no new branch to tell them apart.

The operator check is a correlated NOT EXISTS rather than a JOIN so that the
login's SELECT ... FOR UPDATE keeps locking only the admin row. A JOIN would
lock the operator row too and serialise every sign-in across the operator.

Disabling bumps token_version, which ends every session on the next request
(and makes sure re-enabling can't revive tokens issued before the disable),
and clears the PIN elevation. Re-enabling only clears the flags.

Every action writes an audit row in the caller's transaction. The callers
commit, then send the notice (platform-initiated actions only).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AdminOtpCode, AdminUser, ISPOperator, Reseller
from src.modules import audit

REASON_MAX = 500

PLATFORM_OWNER = "platform_owner"
OPERATOR_USER = "operator_user"


def _operator_logins_enabled(operator_id_column):
    return ~exists().where(
        ISPOperator.id == operator_id_column,
        ISPOperator.admin_logins_disabled_at.is_not(None),
    )


def admin_may_sign_in():
    """WHERE clause: this admin row is active and its operator's logins are on."""
    return and_(AdminUser.is_active == True, _operator_logins_enabled(AdminUser.isp_operator_id))  # noqa: E712


def reseller_may_sign_in():
    """WHERE clause: this reseller is active and its operator's logins are on."""
    return and_(Reseller.is_active == True, _operator_logins_enabled(Reseller.isp_operator_id))  # noqa: E712


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clean_reason(reason: str | None) -> str | None:
    text = (reason or "").strip()
    return text[:REASON_MAX] or None


async def _expire_open_codes(db: AsyncSession, where) -> None:
    """One-time codes (onboarding, reset, phone change, PIN reset) still open
    for the affected admins. Reset grants need nothing here: they carry the
    token_version, which the caller bumps."""
    await db.execute(
        update(AdminOtpCode)
        .where(AdminOtpCode.consumed_at.is_(None), AdminOtpCode.admin_user_id.in_(select(AdminUser.id).where(where)))
        .values(consumed_at=_now())
    )


async def disable_admin(
    db: AsyncSession,
    admin: AdminUser,
    *,
    by_type: str,
    by_id: uuid.UUID,
    reason: str | None,
) -> None:
    """Turn one admin off. Caller holds the row (FOR UPDATE) and commits."""
    reason = clean_reason(reason)
    admin.is_active = False
    admin.disabled_at = _now()
    admin.disabled_by_type = by_type
    admin.disabled_by_id = by_id
    admin.disabled_reason = reason
    admin.token_version = int(admin.token_version or 0) + 1
    admin.pin_verified_until = None
    await _expire_open_codes(db, AdminUser.id == admin.id)
    audit.record(
        db,
        "admin_user.disable",
        isp_operator_id=admin.isp_operator_id,
        target_type="admin_user",
        target_id=admin.id,
        summary=f"Disabled sign-in for {admin.email}",
        detail={"reason": reason},
    )


def enable_admin(db: AsyncSession, admin: AdminUser, *, reason: str | None) -> None:
    """Turn one admin back on: clears the flags, nothing else. Caller commits."""
    previous = {"disabled_by_type": admin.disabled_by_type, "disabled_reason": admin.disabled_reason}
    admin.is_active = True
    admin.disabled_at = None
    admin.disabled_by_type = None
    admin.disabled_by_id = None
    admin.disabled_reason = None
    audit.record(
        db,
        "admin_user.enable",
        isp_operator_id=admin.isp_operator_id,
        target_type="admin_user",
        target_id=admin.id,
        summary=f"Re-enabled sign-in for {admin.email}",
        detail={"reason": clean_reason(reason), "was_disabled_by": previous["disabled_by_type"]},
    )


async def disable_operator_logins(
    db: AsyncSession, operator: ISPOperator, *, owner_id: uuid.UUID, reason: str | None
) -> int:
    """Turn off sign-in for every admin and reseller of ``operator``.

    One UPDATE signs every admin out (token_version) and clears their PIN
    elevation. Resellers have no token_version; the per-request reseller guard
    checks the operator flag, so their sessions end on the next call. Returns
    how many admins were signed out. Caller holds the operator row (FOR
    UPDATE) and commits.
    """
    reason = clean_reason(reason)
    operator.admin_logins_disabled_at = _now()
    operator.admin_logins_disabled_by = owner_id
    operator.admin_logins_disabled_reason = reason
    signed_out = (
        await db.execute(
            update(AdminUser)
            .where(AdminUser.isp_operator_id == operator.id)
            .values(token_version=AdminUser.token_version + 1, pin_verified_until=None)
            .returning(AdminUser.id)
        )
    ).scalars().all()
    await _expire_open_codes(db, AdminUser.isp_operator_id == operator.id)
    resellers = (
        await db.execute(select(func.count()).select_from(Reseller).where(Reseller.isp_operator_id == operator.id))
    ).scalar_one()
    audit.record(
        db,
        "operator.admin_logins_disable",
        isp_operator_id=operator.id,
        target_type="isp_operator",
        target_id=operator.id,
        summary=f"Disabled all sign-in for {operator.name}",
        detail={"reason": reason, "admins_signed_out": len(signed_out), "resellers_blocked": resellers},
    )
    return len(signed_out)


def enable_operator_logins(db: AsyncSession, operator: ISPOperator, *, reason: str | None) -> None:
    """Turn the operator switch back off. Admins disabled one by one stay
    disabled: their is_active is untouched. Caller commits."""
    operator.admin_logins_disabled_at = None
    operator.admin_logins_disabled_by = None
    operator.admin_logins_disabled_reason = None
    audit.record(
        db,
        "operator.admin_logins_enable",
        isp_operator_id=operator.id,
        target_type="isp_operator",
        target_id=operator.id,
        summary=f"Re-enabled sign-in for {operator.name}",
        detail={"reason": clean_reason(reason)},
    )
