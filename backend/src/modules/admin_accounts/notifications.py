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
from src.modules.sms.types import SMSSendResult
from src.utils.phone import mask_phone

logger = logging.getLogger("admin_accounts.notifications")

OTP_TTL_MINUTES = 10


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
    login_url = f"{get_settings().platform_app_url}/admin/login"
    if reason == "reset":
        next_step = (
            "You'll be asked to choose a new password."
            if admin.phone_verified
            else "You'll be asked to verify this phone and choose a new password."
        )
        message = (
            f"IpAdmin: your admin password was reset by platform support and you were signed out. "
            f"Sign in at {login_url} with email {admin.email} and temporary password {temp_password}. "
            f"{next_step}"
        )
    else:
        message = (
            f"IpAdmin: your admin account is ready. Sign in at {login_url} "
            f"with email {admin.email} and temporary password {temp_password}. "
            "You'll be asked to verify this phone and choose your own password."
        )
    return await _send(admin.phone or "", message, kind=f"temp_password_{reason}")


async def send_otp_sms(phone: str, code: str, *, purpose: str) -> SMSSendResult:
    action = "verify your phone" if purpose == "onboarding" else "reset your password"
    message = (
        f"IpAdmin code: {code}. Use it to {action}. "
        f"It expires in {OTP_TTL_MINUTES} minutes. Never share this code."
    )
    return await _send(phone, message, kind=f"otp_{purpose}")
