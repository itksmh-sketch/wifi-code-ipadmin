"""
Dispatcher: fires both email and SMS for each notification event.
Notification failures never raise — always log and continue.

The wording of every message below comes from template_store, which reads the
platform-owner-editable row and falls back to the shipped default in
template_catalog. What stays here is which values each event supplies; the text
those values land in is data, not code.

SMS is no longer truncated to 160 characters on the way out. That cut fell in
the middle of the message whenever a real URL and email pushed it past the
limit — on the approval SMS it could take the temp password with it, which is
the one thing in that message the recipient cannot get anywhere else. Length is
now bounded where it can be seen and fixed: the editor refuses a template whose
rendered segment count exceeds template_catalog.MAX_SMS_SEGMENTS and shows the
count while you type.
"""
from __future__ import annotations
import logging
from decimal import Decimal

from src.modules.notifications.email.service import get_email_service
from src.modules.notifications import template_store as store
from src.modules.platform.settings_service import get_platform_name
from src.config import get_settings

logger = logging.getLogger("notifications.dispatcher")


async def _send_email(to: str, subject: str, html: str, text: str) -> None:
    try:
        svc = get_email_service()
        await svc.send(to=to, subject=subject, body_html=html, body_text=text)
    except Exception as exc:
        logger.error("notification_email_error to=%s subject=%s error=%s", to, subject, exc)


async def _send_sms(to: str, message: str) -> None:
    """Best-effort operator notification SMS.

    Opens its own short-lived session rather than taking a db parameter: the
    9 notify_* functions below are called from 10 sites across 5 modules
    (application service, three jobs, the billing webhook), and threading a
    session through all of them to reach one leaf would be a far larger change
    than this leaf warrants. Same approach sms.metering takes for the same
    reason.

    Credentials are read per send — notifications are low volume, and a cached
    provider could not see a credential change made through the settings card.
    """
    try:
        from src.db.base import async_session_factory
        from src.modules.platform.notification_sms_credentials_service import (
            resolve_notification_sms,
        )
        from src.modules.sms.providers.arkesel import ArkeselSMSProvider

        async with async_session_factory() as db:
            resolved = await resolve_notification_sms(db)

        if resolved is None:
            logger.warning("notification_sms_skipped to=%s reason=no_active_credential", to)
            return

        # Constructed directly rather than through sms.providers.registry: that
        # registry maps an OPERATOR's chosen provider to an implementation, and
        # its three arkesel branches already differ only in credential key
        # naming. This path is never operator-chosen — it is always the
        # platform's own account — so a fourth near-identical branch would add
        # nothing but another thing to keep in sync.
        _, credentials = resolved
        provider = ArkeselSMSProvider(
            api_key=credentials["api_key"], sender_id=credentials["sender_id"]
        )
        result = await provider.send(to=to, message=message)
        if result is not None and not result.success:
            logger.error("notification_sms_failed to=%s error=%s", to, result.error)
    except Exception as exc:
        logger.error("notification_sms_error to=%s error=%s", to, exc)


def _settings():
    return get_settings()


async def _support_email() -> str:
    """The support address applicants are told to write to.

    Read from the platform_settings table, which a platform owner edits in the
    portal's Settings page — config only ever held the placeholder. Own
    short-lived session for the same reason _send_sms and
    template_store._load_safely open one: the notify_* functions have no
    session to borrow. Falls back to config on any failure, so a lookup error
    degrades the address rather than the notification.
    """
    try:
        from src.db.base import async_session_factory
        from src.modules.platform.settings_service import get_setting

        async with async_session_factory() as db:
            return await get_setting(db, "platform_support_email")
    except Exception as exc:
        logger.error("notification_support_email_lookup_failed error=%s — using config", exc)
        return _settings().platform_support_email


async def notify_application_received(*, email: str, contact_name: str, isp_name: str, phone: str) -> None:
    values = {
        "contact_name": contact_name,
        "isp_name": isp_name,
        "support_email": await _support_email(),
        "platform_name": await get_platform_name(),
    }
    subj, html, text = await store.render_email("application_received", values)
    await _send_email(email, subj, html, text)
    await _send_sms(phone, await store.render_sms("application_received", values))


async def notify_application_approved(
    *,
    email: str,
    phone: str,
    contact_name: str,
    isp_name: str,
    admin_email: str,
    temp_password: str,
    trial_days: int,
    send_sms: bool = True,
) -> None:
    """send_sms=False: the caller delivers the temp password by SMS itself (the
    admin-provisioning path needs a real send result, which _send_sms can't give)."""
    values = {
        "contact_name": contact_name,
        "isp_name": isp_name,
        "login_url": f"{_settings().platform_app_url}/admin",
        "admin_email": admin_email,
        "temp_password": temp_password,
        "trial_days": trial_days,
        "platform_name": await get_platform_name(),
    }
    subj, html, text = await store.render_email("application_approved", values)
    await _send_email(email, subj, html, text)
    if not send_sms:
        return
    await _send_sms(phone, await store.render_sms("application_approved", values))


async def notify_application_rejected(
    *,
    email: str,
    phone: str,
    contact_name: str,
    isp_name: str,
    rejection_reason: str,
) -> None:
    values = {
        "contact_name": contact_name,
        "isp_name": isp_name,
        "rejection_reason": rejection_reason,
        "support_email": await _support_email(),
        "platform_name": await get_platform_name(),
    }
    subj, html, text = await store.render_email("application_rejected", values)
    await _send_email(email, subj, html, text)
    await _send_sms(phone, await store.render_sms("application_rejected", values))


async def notify_trial_expiry_warning(
    *,
    email: str,
    phone: str,
    isp_name: str,
    trial_end_date: str,
    days_remaining: int,
    monthly_fee_ghs: Decimal,
) -> None:
    values = {
        "isp_name": isp_name,
        "trial_end_date": trial_end_date,
        "days_remaining": days_remaining,
        "monthly_fee_ghs": monthly_fee_ghs,
        "billing_url": f"{_settings().platform_app_url}/admin/billing",
        "platform_name": await get_platform_name(),
    }
    subj, html, text = await store.render_email("trial_expiry_warning", values)
    await _send_email(email, subj, html, text)
    await _send_sms(phone, await store.render_sms("trial_expiry_warning", values))


async def notify_trial_expired(*, email: str, phone: str, isp_name: str) -> None:
    values = {
        "isp_name": isp_name,
        "billing_url": f"{_settings().platform_app_url}/admin/billing",
        "platform_name": await get_platform_name(),
    }
    subj, html, text = await store.render_email("trial_expired", values)
    await _send_email(email, subj, html, text)
    await _send_sms(phone, await store.render_sms("trial_expired", values))


async def notify_invoice_issued(
    *,
    email: str,
    phone: str,
    isp_name: str,
    invoice_number: str,
    amount_ghs: Decimal,
    period_start: str,
    period_end: str,
    due_date: str,
    payment_url: str,
) -> None:
    values = {
        "isp_name": isp_name,
        "invoice_number": invoice_number,
        "amount_ghs": amount_ghs,
        "period_start": period_start,
        "period_end": period_end,
        "due_date": due_date,
        "payment_url": payment_url,
        "platform_name": await get_platform_name(),
    }
    subj, html, text = await store.render_email("invoice_issued", values)
    await _send_email(email, subj, html, text)
    await _send_sms(phone, await store.render_sms("invoice_issued", values))


async def notify_grace_period(
    *,
    email: str,
    phone: str,
    isp_name: str,
    invoice_number: str,
    amount_ghs: Decimal,
    suspension_date: str,
    payment_url: str,
) -> None:
    values = {
        "isp_name": isp_name,
        "invoice_number": invoice_number,
        "amount_ghs": amount_ghs,
        "suspension_date": suspension_date,
        "payment_url": payment_url,
        "platform_name": await get_platform_name(),
    }
    subj, html, text = await store.render_email("grace_period", values)
    await _send_email(email, subj, html, text)
    await _send_sms(phone, await store.render_sms("grace_period", values))


async def notify_suspended(*, email: str, phone: str, isp_name: str) -> None:
    values = {
        "isp_name": isp_name,
        "payment_url": f"{_settings().platform_app_url}/admin/billing",
        "platform_name": await get_platform_name(),
    }
    subj, html, text = await store.render_email("account_suspended", values)
    await _send_email(email, subj, html, text)
    await _send_sms(phone, await store.render_sms("account_suspended", values))


async def notify_reactivated(*, email: str, phone: str, isp_name: str, next_invoice_date: str) -> None:
    values = {
        "isp_name": isp_name,
        "next_invoice_date": next_invoice_date,
        "platform_name": await get_platform_name(),
    }
    subj, html, text = await store.render_email("account_reactivated", values)
    await _send_email(email, subj, html, text)
    await _send_sms(phone, await store.render_sms("account_reactivated", values))
