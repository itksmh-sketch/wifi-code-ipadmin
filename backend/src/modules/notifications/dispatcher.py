"""
Dispatcher: fires both email and SMS for each notification event.
Notification failures never raise — always log and continue.
"""
from __future__ import annotations
import logging
from decimal import Decimal

from src.modules.notifications.email.service import get_email_service
from src.modules.notifications.email import templates as t
from src.modules.sms.dependencies import get_sms_service
from src.config import get_settings

logger = logging.getLogger("notifications.dispatcher")


async def _send_email(to: str, subject: str, html: str, text: str) -> None:
    try:
        svc = get_email_service()
        await svc.send(to=to, subject=subject, body_html=html, body_text=text)
    except Exception as exc:
        logger.error("notification_email_error to=%s subject=%s error=%s", to, subject, exc)


async def _send_sms(to: str, message: str) -> None:
    try:
        svc = get_sms_service()
        await svc.send(to=to, message=message)
    except Exception as exc:
        logger.error("notification_sms_error to=%s error=%s", to, exc)


def _settings():
    return get_settings()


async def notify_application_received(*, email: str, contact_name: str, isp_name: str, phone: str) -> None:
    s = _settings()
    subj, html, text = t.application_received(
        contact_name=contact_name, isp_name=isp_name, support_email=s.platform_support_email
    )
    await _send_email(email, subj, html, text)
    sms = (
        f"Hi {contact_name}, your application to join YourISP Platform has been received. "
        f"We'll be in touch within 48 hours. Questions? {s.platform_support_email}"
    )
    await _send_sms(phone, sms)


async def notify_application_approved(
    *,
    email: str,
    phone: str,
    contact_name: str,
    isp_name: str,
    admin_email: str,
    temp_password: str,
    trial_days: int,
) -> None:
    s = _settings()
    login_url = f"{s.platform_app_url}/admin"
    subj, html, text = t.application_approved(
        contact_name=contact_name,
        isp_name=isp_name,
        login_url=login_url,
        admin_email=admin_email,
        temp_password=temp_password,
        trial_days=trial_days,
    )
    await _send_email(email, subj, html, text)
    sms = (
        f"Welcome to YourISP Platform! Your ISP account is approved. "
        f"Login: {login_url} | Email: {admin_email} | Temp password: {temp_password} | "
        f"{trial_days}-day free trial starts now."
    )
    await _send_sms(phone, sms[:160])


async def notify_application_rejected(
    *,
    email: str,
    phone: str,
    contact_name: str,
    isp_name: str,
    rejection_reason: str,
) -> None:
    s = _settings()
    subj, html, text = t.application_rejected(
        contact_name=contact_name,
        isp_name=isp_name,
        rejection_reason=rejection_reason,
        support_email=s.platform_support_email,
    )
    await _send_email(email, subj, html, text)
    sms = (
        f"Hi {contact_name}, unfortunately we cannot approve your YourISP Platform application "
        f"at this time. Check your email for details."
    )
    await _send_sms(phone, sms)


async def notify_trial_expiry_warning(
    *,
    email: str,
    phone: str,
    isp_name: str,
    trial_end_date: str,
    days_remaining: int,
    monthly_fee_ghs: Decimal,
) -> None:
    s = _settings()
    billing_url = f"{s.platform_app_url}/admin/billing"
    subj, html, text = t.trial_expiry_warning(
        isp_name=isp_name,
        trial_end_date=trial_end_date,
        days_remaining=days_remaining,
        monthly_fee_ghs=monthly_fee_ghs,
        billing_url=billing_url,
    )
    await _send_email(email, subj, html, text)
    sms = (
        f"Your YourISP Platform trial ends in {days_remaining} days. "
        f"Monthly fee: GHS {monthly_fee_ghs}. Pay at: {billing_url} to keep full access."
    )
    await _send_sms(phone, sms[:160])


async def notify_trial_expired(*, email: str, phone: str, isp_name: str) -> None:
    s = _settings()
    billing_url = f"{s.platform_app_url}/admin/billing"
    subj, html, text = t.trial_expired(isp_name=isp_name, billing_url=billing_url)
    await _send_email(email, subj, html, text)
    sms = (
        f"Your YourISP Platform trial has ended. Account is now read-only. "
        f"Pay your invoice to restore access: {billing_url}"
    )
    await _send_sms(phone, sms[:160])


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
    subj, html, text = t.invoice_issued(
        isp_name=isp_name,
        invoice_number=invoice_number,
        amount_ghs=amount_ghs,
        period_start=period_start,
        period_end=period_end,
        due_date=due_date,
        payment_url=payment_url,
    )
    await _send_email(email, subj, html, text)
    sms = f"Invoice {invoice_number} for GHS {amount_ghs} due {due_date}. Pay: {payment_url}"
    await _send_sms(phone, sms[:160])


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
    subj, html, text = t.grace_period_warning(
        isp_name=isp_name,
        invoice_number=invoice_number,
        amount_ghs=amount_ghs,
        suspension_date=suspension_date,
        payment_url=payment_url,
    )
    await _send_email(email, subj, html, text)
    sms = (
        f"YourISP Platform: Invoice {invoice_number} overdue. "
        f"Pay GHS {amount_ghs} by {suspension_date} to avoid suspension: {payment_url}"
    )
    await _send_sms(phone, sms[:160])


async def notify_suspended(*, email: str, phone: str, isp_name: str) -> None:
    s = _settings()
    payment_url = f"{s.platform_app_url}/admin/billing"
    subj, html, text = t.account_suspended(isp_name=isp_name, payment_url=payment_url)
    await _send_email(email, subj, html, text)
    sms = (
        f"YourISP Platform: Account suspended. "
        f"Existing sessions continue but no new vouchers/payments. Pay to restore: {payment_url}"
    )
    await _send_sms(phone, sms[:160])


async def notify_reactivated(*, email: str, phone: str, isp_name: str, next_invoice_date: str) -> None:
    subj, html, text = t.account_reactivated(isp_name=isp_name, next_invoice_date=next_invoice_date)
    await _send_email(email, subj, html, text)
    sms = f"YourISP Platform: Payment received! Full access restored. Next invoice: {next_invoice_date}."
    await _send_sms(phone, sms[:160])
