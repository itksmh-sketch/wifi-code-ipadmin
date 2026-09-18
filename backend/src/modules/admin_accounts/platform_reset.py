"""Platform-owner-initiated password reset for an operator admin.

Two outcomes, decided by whether the admin has a verified phone:

* temp_password — phone verified: a new temp password is set and texted to that
  phone; must_change_password sends the admin straight to "choose a new
  password" on next sign-in. The platform owner never sees the password.
* onboarding    — no verified phone: an optional new (unverified) phone is
  stored, a temp password is set, and must_complete_onboarding sends the admin
  back through the full phone + OTP + password flow, exactly like a new
  account. The temp password is returned once to the platform owner, because
  an unverified number may not reach the admin.

Either way token_version is bumped, so every existing session and refresh
token dies immediately, and an AdminPasswordResetEvent records who did it.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AdminOtpCode, AdminPasswordResetEvent, AdminUser
from src.modules.admin_accounts import notifications
from src.modules.admin_accounts.provisioning import generate_temp_password
from src.utils.auth import hash_password
from src.utils.phone import mask_phone, normalize_ghana_phone

logger = logging.getLogger("admin_accounts.platform_reset")

RESET_RATE_LIMIT = 5
RESET_RATE_WINDOW_SECONDS = 3600


class VerifiedPhoneConflict(ValueError):
    """A phone was supplied for an admin whose phone is already verified."""


@dataclass
class ResetOutcome:
    mode: str
    sms_sent: bool
    sms_error: str | None
    phone_changed: bool
    temp_password: str | None  # only for mode == "onboarding"
    event_id: uuid.UUID


async def reset_admin_password(
    db: AsyncSession,
    *,
    admin: AdminUser,
    platform_owner_id: uuid.UUID,
    phone: str | None,
) -> ResetOutcome:
    """Reset and commit, then text the temp password (send-after-commit).

    ``admin`` should be loaded FOR UPDATE by the caller. Raises ValueError for a
    malformed phone and VerifiedPhoneConflict if a phone is supplied for an
    admin whose phone is already verified (their verified number is the only
    place a temp password may go).
    """
    verified = bool(admin.phone_verified and admin.phone)
    new_phone = normalize_ghana_phone(phone) if phone else None
    if verified and new_phone:
        raise VerifiedPhoneConflict(
            "This admin has a verified phone; the new temporary password is sent there. Leave the phone empty."
        )

    temp_password = generate_temp_password()
    phone_changed = False
    if verified:
        mode = "temp_password"
        admin.must_change_password = True
    else:
        mode = "onboarding"
        if new_phone:
            phone_changed = new_phone != admin.phone
            admin.phone = new_phone
        admin.phone_verified = False
        admin.must_complete_onboarding = True
        admin.must_change_password = False

    admin.password_hash = hash_password(temp_password)
    # Kills every access/refresh token and outstanding reset grant right now.
    admin.token_version = int(admin.token_version or 0) + 1
    now = datetime.now(timezone.utc)
    # Codes issued before the reset (possibly to an old number) are void.
    await db.execute(
        update(AdminOtpCode)
        .where(AdminOtpCode.admin_user_id == admin.id, AdminOtpCode.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    event = AdminPasswordResetEvent(
        admin_user_id=admin.id,
        isp_operator_id=admin.isp_operator_id,
        platform_owner_id=platform_owner_id,
        mode=mode,
        phone_changed=phone_changed,
        sms_sent=False,
    )
    db.add(event)
    await db.commit()

    # Only after the commit: never text credentials the database doesn't hold.
    result = await notifications.send_temp_password_sms(admin, temp_password, reason="reset")
    event.sms_sent = bool(result.success)
    event.sms_error = None if result.success else (result.error or "sms_failed")[:500]
    await db.commit()

    logger.warning(
        "admin_password_reset_by_platform_owner platform_owner_id=%s admin_id=%s operator_id=%s mode=%s "
        "phone_changed=%s sms_sent=%s phone=%s event_id=%s",
        platform_owner_id, admin.id, admin.isp_operator_id, mode, phone_changed,
        event.sms_sent, mask_phone(admin.phone), event.id,
    )
    return ResetOutcome(
        mode=mode,
        sms_sent=event.sms_sent,
        sms_error=event.sms_error,
        phone_changed=phone_changed,
        temp_password=temp_password if mode == "onboarding" else None,
        event_id=event.id,
    )
