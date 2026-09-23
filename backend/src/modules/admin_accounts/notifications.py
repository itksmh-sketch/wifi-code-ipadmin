"""SMS sends for admin onboarding and password reset.

Unlike notifications.dispatcher._send_sms (best-effort, fire-and-forget), these
return a real SMSSendResult: the platform owner needs to know whether the temp
password actually went out, and an OTP endpoint must not claim "code sent"
when it wasn't. Sends go through the platform's own notification SMS account.
"""
from __future__ import annotations

import logging

from src.config import get_settings
from src.db.base import async_session_factory
from src.db.models import AdminUser
from src.modules.platform.settings_service import get_platform_name
from src.modules.sms.types import SMSSendResult
from src.utils.phone import mask_phone

logger = logging.getLogger("admin_accounts.notifications")

OTP_TTL_MINUTES = 10


async def _login_url() -> str:
    """The admin sign-in link to put in an SMS.

    Read from the platform_settings table, not straight from config: that row
    is the source of truth a platform owner edits in the portal's Settings
    page, and on this deployment it holds the public HTTPS domain while the
    .env value is the bare server address. Every link an operator receives
    should be the one they can bookmark. Falls back to config (which is also
    get_setting's own default when the row is empty) if the lookup fails, so a
    database hiccup degrades the link rather than the security alert carrying
    it.
    """
    from src.modules.platform.settings_service import get_setting

    try:
        async with async_session_factory() as db:
            base = await get_setting(db, "platform_app_url")
    except Exception as exc:
        logger.error("admin_sms_app_url_lookup_failed error=%s", exc)
        base = get_settings().platform_app_url
    return f"{base.rstrip('/')}/admin/login"


async def _send(to: str, message: str, *, kind: str) -> SMSSendResult:
    if not to:
        return SMSSendResult(success=False, error="no_phone_on_file")
    try:
        from src.modules.platform.notification_sms_credentials_service import resolve_notification_sms
        from src.modules.sms.providers.arkesel import ArkeselSMSProvider

        async with async_session_factory() as db:
            resolved = await resolve_notification_sms(db)
        if resolved is None:
            logger.error("admin_sms_not_configured kind=%s to=%s", kind, mask_phone(to))
            return SMSSendResult(success=False, error="sms_not_configured")
        _, credentials = resolved
        provider = ArkeselSMSProvider(api_key=credentials["api_key"], sender_id=credentials["sender_id"])
        result = await provider.send(to=to, message=message)
    except Exception as exc:  # a send must never break the request that triggered it
        logger.error("admin_sms_error kind=%s to=%s error=%s", kind, mask_phone(to), exc)
        return SMSSendResult(success=False, error="sms_send_error")
    if result.success:
        logger.info("admin_sms_sent kind=%s to=%s", kind, mask_phone(to))
    else:
        logger.error("admin_sms_failed kind=%s to=%s error=%s", kind, mask_phone(to), result.error)
    return result


async def send_temp_password_sms(admin: AdminUser, temp_password: str, *, reason: str = "new") -> SMSSendResult:
    """reason: "new" (account just created) or "reset" (a platform owner reset it)."""
    login_url = await _login_url()
    name = await get_platform_name()
    if reason == "reset":
        next_step = (
            "You'll be asked to choose a new password."
            if admin.phone_verified
            else "You'll be asked to verify this phone and choose a new password."
        )
        message = (
            f"{name}: your admin password was reset by platform support and you were signed out. "
            f"Sign in at {login_url} with email {admin.email} and temporary password {temp_password}. "
            f"{next_step}"
        )
    else:
        message = (
            f"{name}: your admin account is ready. Sign in at {login_url} "
            f"with email {admin.email} and temporary password {temp_password}. "
            "You'll be asked to verify this phone and choose your own password."
        )
    return await _send(admin.phone or "", message, kind=f"temp_password_{reason}")


# What the code in the SMS is for, keyed by AdminOtpCode.purpose. A purpose
# with no entry falls back to the password-reset wording, which is what every
# non-onboarding code used to say.
_OTP_ACTIONS = {
    "onboarding": "verify your phone",
    "reset": "reset your password",
    "pin_reset": "reset your PIN",
    "phone_change": "confirm your new phone number",
}


async def send_otp_sms(phone: str, code: str, *, purpose: str) -> SMSSendResult:
    action = _OTP_ACTIONS.get(purpose, "reset your password")
    name = await get_platform_name()
    message = (
        f"{name} code: {code}. Use it to {action}. "
        f"It expires in {OTP_TTL_MINUTES} minutes. Never share this code."
    )
    return await _send(phone, message, kind=f"otp_{purpose}")


async def send_lockout_sms(admin: AdminUser, *, kind: str) -> SMSSendResult:
    """Tell an admin their account or PIN just locked.

    A fixed string, not a platform_notification_templates row. That catalog
    holds operator-lifecycle business mail (applications, trials, invoices,
    suspension) addressed to the operator, and is platform-owner editable; the
    security-critical per-admin sends — OTP and temp password — were kept out
    of it deliberately, and this belongs with those. An editable security alert
    is one bad edit away from dropping the "if this wasn't you" line for every
    operator at once.

    kind: "login" (5 failed sign-ins) or "pin" (10 wrong PIN entries).
    """
    from src.modules.admin_accounts.lockout import LOCKOUT_HOURS, LOGIN_MAX_ATTEMPTS, PIN_MAX_ATTEMPTS

    login_url = await _login_url()
    name = await get_platform_name()
    if kind == "pin":
        message = (
            f"{name} security: your PIN was locked for {LOCKOUT_HOURS} hours after "
            f"{PIN_MAX_ATTEMPTS} incorrect entries, and you were signed out everywhere. "
            f"If this wasn't you, change your password now at {login_url}."
        )
    else:
        message = (
            f"{name} security: your account was locked for {LOCKOUT_HOURS} hours after "
            f"{LOGIN_MAX_ATTEMPTS} failed sign-in attempts. "
            f"If this wasn't you, reset your password at {login_url} once the lock clears."
        )
    return await _send(admin.phone or "", message, kind=f"lockout_{kind}")


async def send_phone_changed_sms(old_phone: str, admin: AdminUser) -> SMSSendResult:
    """Tell the OLD number that the account's phone was just moved.

    This is not a courtesy notice, it is the control that makes a phone change
    safe to expose to a signed-in session at all. The verified phone is the
    recovery channel for both forgot-password and forgot-PIN, so anyone who
    reaches a live session and silently repoints it owns the account outright:
    change the number, then "forget" the password, and every factor now lands
    on their handset. Texting the number being replaced is the one message such
    an attacker cannot intercept, and it goes out before they can benefit.
    """
    login_url = await _login_url()
    name = await get_platform_name()
    message = (
        f"{name} security: the phone number on admin account {admin.email} was just changed to a "
        f"different number, so alerts and reset codes will no longer come here. "
        f"If this wasn't you, sign in at {login_url} and change your password immediately."
    )
    return await _send(old_phone, message, kind="phone_changed_old_number")
