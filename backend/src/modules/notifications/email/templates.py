"""All email templates as (subject, html, text) tuples."""
from __future__ import annotations
from decimal import Decimal


def _wrap(body: str) -> str:
    return f"<html><body style='font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px'>{body}</body></html>"


def application_received(*, contact_name: str, isp_name: str, support_email: str, platform_name: str = "YourISP Platform") -> tuple[str, str, str]:
    subject = f"We received your application — {isp_name}"
    html = _wrap(
        f"<h2>Hi {contact_name},</h2>"
        f"<p>Thank you for applying to join <strong>{platform_name}</strong>.</p>"
        f"<p>Your application for <strong>{isp_name}</strong> has been received. "
        f"Our team will review it and contact you within 24-48 hours.</p>"
        f"<p>Questions? Email us at <a href='mailto:{support_email}'>{support_email}</a></p>"
        f"<p>Regards,<br>The {platform_name} Team</p>"
    )
    text = (
        f"Hi {contact_name},\n\nThank you for applying to {platform_name}.\n"
        f"Your application for {isp_name} has been received. We'll review it and contact you within 24-48 hours.\n"
        f"Questions? Email {support_email}\n\nRegards,\nThe {platform_name} Team"
    )
    return subject, html, text


def application_approved(
    *,
    contact_name: str,
    isp_name: str,
    login_url: str,
    admin_email: str,
    temp_password: str,
    trial_days: int,
    platform_name: str = "YourISP Platform",
) -> tuple[str, str, str]:
    subject = f"Your ISP account is approved — welcome to {platform_name}"
    html = _wrap(
        f"<h2>Welcome to {platform_name}, {contact_name}!</h2>"
        f"<p>Your application for <strong>{isp_name}</strong> has been approved.</p>"
        f"<h3>Login details</h3>"
        f"<table><tr><td><strong>URL:</strong></td><td><a href='{login_url}'>{login_url}</a></td></tr>"
        f"<tr><td><strong>Email:</strong></td><td>{admin_email}</td></tr>"
        f"<tr><td><strong>Temp password:</strong></td><td><code>{temp_password}</code></td></tr></table>"
        f"<p>You have a <strong>{trial_days}-day free trial</strong> starting now.</p>"
        f"<h3>Getting started</h3><ol>"
        f"<li>Add your first town and site</li>"
        f"<li>Register your MikroTik router</li>"
        f"<li>Configure your Paystack payment credentials</li>"
        f"<li>Generate your first vouchers</li>"
        f"</ol>"
        f"<p>Change your password on first login.</p>"
    )
    text = (
        f"Welcome to {platform_name}, {contact_name}!\n\n"
        f"Your account for {isp_name} has been approved.\n\n"
        f"Login URL: {login_url}\nEmail: {admin_email}\nTemp password: {temp_password}\n\n"
        f"You have a {trial_days}-day free trial starting now.\nChange your password on first login."
    )
    return subject, html, text


def application_rejected(
    *,
    contact_name: str,
    isp_name: str,
    rejection_reason: str,
    support_email: str,
    platform_name: str = "YourISP Platform",
) -> tuple[str, str, str]:
    subject = f"Your application to {platform_name}"
    html = _wrap(
        f"<h2>Hi {contact_name},</h2>"
        f"<p>Thank you for your interest in <strong>{platform_name}</strong>.</p>"
        f"<p>Unfortunately, we are unable to approve your application for <strong>{isp_name}</strong> at this time.</p>"
        f"<p><strong>Reason:</strong> {rejection_reason}</p>"
        f"<p>You are welcome to reapply if your circumstances change. "
        f"If you have questions, contact us at <a href='mailto:{support_email}'>{support_email}</a>.</p>"
    )
    text = (
        f"Hi {contact_name},\n\nThank you for applying to {platform_name}.\n"
        f"Unfortunately we cannot approve your application for {isp_name} at this time.\n"
        f"Reason: {rejection_reason}\n\n"
        f"You are welcome to reapply if circumstances change. Questions? {support_email}"
    )
    return subject, html, text


def trial_expiry_warning(
    *,
    isp_name: str,
    trial_end_date: str,
    days_remaining: int,
    monthly_fee_ghs: Decimal,
    billing_url: str,
    platform_name: str = "YourISP Platform",
) -> tuple[str, str, str]:
    subject = f"Your free trial ends in {days_remaining} days — {isp_name}"
    html = _wrap(
        f"<h2>Your trial ends soon, {isp_name}</h2>"
        f"<p>Your <strong>{platform_name}</strong> free trial ends on <strong>{trial_end_date}</strong> "
        f"({days_remaining} days remaining).</p>"
        f"<p>Monthly fee: <strong>GHS {monthly_fee_ghs}</strong></p>"
        f"<p>After the trial, your account will switch to read-only mode until you pay your first invoice.</p>"
        f"<p><a href='{billing_url}' style='background:#2563eb;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px'>Pay Now</a></p>"
    )
    text = (
        f"Your {platform_name} trial for {isp_name} ends on {trial_end_date} ({days_remaining} days remaining).\n"
        f"Monthly fee: GHS {monthly_fee_ghs}\n"
        f"Pay at: {billing_url}\n"
        f"After the trial, your account enters read-only mode until payment."
    )
    return subject, html, text


def trial_expired(
    *,
    isp_name: str,
    billing_url: str,
    platform_name: str = "YourISP Platform",
) -> tuple[str, str, str]:
    subject = "Your trial has ended — action required"
    html = _wrap(
        f"<h2>Your trial has ended, {isp_name}</h2>"
        f"<p>Your <strong>{platform_name}</strong> free trial has ended. "
        f"Your account is now in <strong>read-only mode</strong>.</p>"
        f"<p>Pay your invoice to restore full access:</p>"
        f"<p><a href='{billing_url}' style='background:#2563eb;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px'>Pay Invoice</a></p>"
    )
    text = (
        f"Your {platform_name} trial for {isp_name} has ended.\n"
        f"Your account is now in read-only mode.\n"
        f"Pay your invoice to restore access: {billing_url}"
    )
    return subject, html, text


def invoice_issued(
    *,
    isp_name: str,
    invoice_number: str,
    amount_ghs: Decimal,
    period_start: str,
    period_end: str,
    due_date: str,
    payment_url: str,
    platform_name: str = "YourISP Platform",
) -> tuple[str, str, str]:
    subject = f"Invoice {invoice_number} — GHS {amount_ghs} due {due_date}"
    html = _wrap(
        f"<h2>Invoice {invoice_number}</h2>"
        f"<table style='width:100%;border-collapse:collapse'>"
        f"<tr><td>Operator:</td><td><strong>{isp_name}</strong></td></tr>"
        f"<tr><td>Period:</td><td>{period_start} – {period_end}</td></tr>"
        f"<tr><td>Amount:</td><td><strong>GHS {amount_ghs}</strong></td></tr>"
        f"<tr><td>Due:</td><td><strong>{due_date}</strong></td></tr>"
        f"</table>"
        f"<br><a href='{payment_url}' style='background:#2563eb;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px'>Pay Now</a>"
        f"<p>If payment is not received by {due_date}, a grace period will begin. "
        f"Accounts unpaid after the grace period will be suspended.</p>"
    )
    text = (
        f"Invoice {invoice_number} for {isp_name}\n"
        f"Period: {period_start} – {period_end}\n"
        f"Amount: GHS {amount_ghs}\n"
        f"Due: {due_date}\n"
        f"Pay at: {payment_url}"
    )
    return subject, html, text


def grace_period_warning(
    *,
    isp_name: str,
    invoice_number: str,
    amount_ghs: Decimal,
    suspension_date: str,
    payment_url: str,
    platform_name: str = "YourISP Platform",
) -> tuple[str, str, str]:
    subject = "Payment overdue — 14 days until suspension"
    html = _wrap(
        f"<h2>Invoice overdue — {isp_name}</h2>"
        f"<p>Invoice <strong>{invoice_number}</strong> for GHS <strong>{amount_ghs}</strong> is overdue.</p>"
        f"<p>Your account will be <strong>suspended on {suspension_date}</strong> if payment is not received.</p>"
        f"<p><a href='{payment_url}' style='background:#dc2626;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px'>Pay Now to Avoid Suspension</a></p>"
    )
    text = (
        f"Invoice {invoice_number} for {isp_name} is overdue.\n"
        f"Amount: GHS {amount_ghs}\n"
        f"Account suspended on: {suspension_date}\n"
        f"Pay at: {payment_url}"
    )
    return subject, html, text


def account_suspended(
    *,
    isp_name: str,
    payment_url: str,
    platform_name: str = "YourISP Platform",
) -> tuple[str, str, str]:
    subject = f"Account suspended — {isp_name}"
    html = _wrap(
        f"<h2>Account suspended — {isp_name}</h2>"
        f"<p>Your <strong>{platform_name}</strong> account has been suspended due to non-payment.</p>"
        f"<p><strong>What this means:</strong></p>"
        f"<ul><li>Existing active vouchers and sessions continue working</li>"
        f"<li>No new vouchers can be generated</li>"
        f"<li>No new payments via captive portal</li></ul>"
        f"<p>Pay your outstanding invoice to restore full access:</p>"
        f"<p><a href='{payment_url}' style='background:#2563eb;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px'>Pay to Reactivate</a></p>"
    )
    text = (
        f"Your {platform_name} account for {isp_name} has been suspended.\n"
        f"Existing sessions continue but no new vouchers or payments.\n"
        f"Pay to restore access: {payment_url}"
    )
    return subject, html, text


def account_reactivated(
    *,
    isp_name: str,
    next_invoice_date: str,
    platform_name: str = "YourISP Platform",
) -> tuple[str, str, str]:
    subject = "Account reactivated — welcome back"
    html = _wrap(
        f"<h2>Welcome back, {isp_name}!</h2>"
        f"<p>Your payment has been confirmed and full access to <strong>{platform_name}</strong> has been restored.</p>"
        f"<p>Next invoice date: <strong>{next_invoice_date}</strong></p>"
    )
    text = (
        f"Welcome back, {isp_name}!\n"
        f"Payment confirmed. Full access to {platform_name} restored.\n"
        f"Next invoice date: {next_invoice_date}"
    )
    return subject, html, text
