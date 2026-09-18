"""The platform's own notification texts: definitions, shipped defaults, render.

Every message the platform sends an operator — application received / approved /
rejected, trial and billing warnings, suspension, reactivation — used to be an
f-string inside dispatcher.py or email/templates.py. It is now a row in
``platform_notification_templates`` that a platform owner can edit, seeded from
exactly the text those f-strings produced.

The defaults below are that text, transcribed with each interpolated value
turned into a ``{name}`` placeholder. tests/test_notification_templates.py
renders every default against the original functions in email/templates.py and
asserts the output is character-identical, so "the shipped default is what we
used to send" is checked rather than claimed.

Stored text is edited input, so substitution goes through utils.templating
(str.format_map over a Mapping that only yields strings) — never an f-string,
never str.format. HTML bodies substitute HTML-escaped values, so a rejection
reason containing ``<`` or ``&`` cannot break out of the markup around it; the
template's own tags are untouched.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.modules.sms.segmentation import count_sms_segments
from src.utils.templating import (
    brace_error,
    escape_values,
    field_access_error,
    placeholders_in,
    safe_format,
)

EMAIL = "email"
SMS = "sms"
CHANNELS = (EMAIL, SMS)

# No platform-name setting exists; this is the value the email defaults have
# always carried, so {platform_name} renders today's text unchanged.
DEFAULT_PLATFORM_NAME = "YourISP Platform"

# A platform notification may legitimately need two segments (the approval SMS
# carries a URL, a login email and a temp password). The cap exists to stop an
# edit quietly turning every billing reminder into a four-part message; the
# exact count is always shown in the editor's preview.
MAX_SMS_SEGMENTS = 3
MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 8000

_HTML_HEAD = "<html><body style='font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px'>"
_HTML_TAIL = "</body></html>"


def wrap_html(inner: str) -> str:
    """The page chrome around every email body — not editable, not stored."""
    return f"{_HTML_HEAD}{inner}{_HTML_TAIL}"


PLACEHOLDER_HELP: dict[str, str] = {
    "platform_name": "The platform's name",
    "support_email": "The platform support email address",
    "contact_name": "The contact person named on the application",
    "isp_name": "The operator's business name",
    "login_url": "Link to the operator admin sign-in page",
    "admin_email": "The new admin's sign-in email",
    "temp_password": "The one-time password the new admin signs in with",
    "trial_days": "Length of the free trial, in days",
    "rejection_reason": "The reason the application was declined",
    "trial_end_date": "The date the trial ends",
    "days_remaining": "Days left in the trial",
    "monthly_fee_ghs": "The monthly subscription fee, in GHS",
    "billing_url": "Link to the operator's billing page",
    "invoice_number": "The invoice reference",
    "amount_ghs": "The invoice amount, in GHS",
    "period_start": "First day of the billing period",
    "period_end": "Last day of the billing period",
    "due_date": "The date the invoice falls due",
    "payment_url": "Link to pay the invoice",
    "suspension_date": "The date the account will be suspended",
    "next_invoice_date": "The date of the next invoice",
}


@dataclass(frozen=True)
class EventDef:
    event: str
    label: str
    description: str
    placeholders: tuple[str, ...]
    required: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemplateDef:
    event: str
    channel: str
    subject: str | None
    body_text: str
    body_html: str | None


# ── events ────────────────────────────────────────────────────────────────

EVENTS: dict[str, EventDef] = {
    "application_received": EventDef(
        event="application_received",
        label="Application received",
        description="Sent the moment someone applies to join the platform.",
        placeholders=("contact_name", "isp_name", "support_email", "platform_name"),
    ),
    "application_approved": EventDef(
        event="application_approved",
        label="Application approved",
        description="Sent when an application is approved. Carries the new admin's sign-in details.",
        placeholders=(
            "contact_name", "isp_name", "login_url", "admin_email",
            "temp_password", "trial_days", "platform_name",
        ),
        # Without it the new admin has no way in: the temp password appears
        # nowhere else, and is not recoverable once this send has gone out.
        required=("temp_password",),
    ),
    "application_rejected": EventDef(
        event="application_rejected",
        label="Application declined",
        description="Sent when an application is turned down.",
        placeholders=("contact_name", "isp_name", "rejection_reason", "support_email", "platform_name"),
    ),
    "trial_expiry_warning": EventDef(
        event="trial_expiry_warning",
        label="Trial ending soon",
        description="Sent while an operator's free trial is still running.",
        placeholders=(
            "isp_name", "trial_end_date", "days_remaining",
            "monthly_fee_ghs", "billing_url", "platform_name",
        ),
    ),
    "trial_expired": EventDef(
        event="trial_expired",
        label="Trial ended",
        description="Sent when the free trial ends and the account goes read-only.",
        placeholders=("isp_name", "billing_url", "platform_name"),
    ),
    "invoice_issued": EventDef(
        event="invoice_issued",
        label="Invoice issued",
        description="Sent with each monthly invoice.",
        placeholders=(
            "isp_name", "invoice_number", "amount_ghs", "period_start",
            "period_end", "due_date", "payment_url", "platform_name",
        ),
    ),
    "grace_period": EventDef(
        event="grace_period",
        label="Payment overdue",
        description="Sent when an invoice passes its due date and the grace period starts.",
        placeholders=(
            "isp_name", "invoice_number", "amount_ghs",
            "suspension_date", "payment_url", "platform_name",
        ),
    ),
    "account_suspended": EventDef(
        event="account_suspended",
        label="Account suspended",
        description="Sent when an unpaid account is suspended.",
        placeholders=("isp_name", "payment_url", "platform_name"),
    ),
    "account_reactivated": EventDef(
        event="account_reactivated",
        label="Account reactivated",
        description="Sent when payment clears and full access is restored.",
        placeholders=("isp_name", "next_invoice_date", "platform_name"),
    ),
}


# ── shipped defaults (transcribed from email/templates.py + dispatcher.py) ──

DEFAULTS: dict[tuple[str, str], TemplateDef] = {}


def _default(event: str, channel: str, *, subject=None, body_text: str, body_html=None) -> None:
    DEFAULTS[(event, channel)] = TemplateDef(
        event=event, channel=channel, subject=subject, body_text=body_text, body_html=body_html
    )


_default(
    "application_received", EMAIL,
    subject="We received your application — {isp_name}",
    body_html=(
        "<h2>Hi {contact_name},</h2>"
        "<p>Thank you for applying to join <strong>{platform_name}</strong>.</p>"
        "<p>Your application for <strong>{isp_name}</strong> has been received. "
        "Our team will review it and contact you within 24-48 hours.</p>"
        "<p>Questions? Email us at <a href='mailto:{support_email}'>{support_email}</a></p>"
        "<p>Regards,<br>The {platform_name} Team</p>"
    ),
    body_text=(
        "Hi {contact_name},\n\nThank you for applying to {platform_name}.\n"
        "Your application for {isp_name} has been received. We'll review it and contact you within 24-48 hours.\n"
        "Questions? Email {support_email}\n\nRegards,\nThe {platform_name} Team"
    ),
)
_default(
    "application_received", SMS,
    body_text=(
        "Hi {contact_name}, your application to join {platform_name} has been received. "
        "We'll be in touch within 48 hours. Questions? {support_email}"
    ),
)

_default(
    "application_approved", EMAIL,
    subject="Your ISP account is approved — welcome to {platform_name}",
    body_html=(
        "<h2>Welcome to {platform_name}, {contact_name}!</h2>"
        "<p>Your application for <strong>{isp_name}</strong> has been approved.</p>"
        "<h3>Login details</h3>"
        "<table><tr><td><strong>URL:</strong></td><td><a href='{login_url}'>{login_url}</a></td></tr>"
        "<tr><td><strong>Email:</strong></td><td>{admin_email}</td></tr>"
        "<tr><td><strong>Temp password:</strong></td><td><code>{temp_password}</code></td></tr></table>"
        "<p>You have a <strong>{trial_days}-day free trial</strong> starting now.</p>"
        "<h3>Getting started</h3><ol>"
        "<li>Add your first town and site</li>"
        "<li>Register your MikroTik router</li>"
        "<li>Configure your Paystack payment credentials</li>"
        "<li>Generate your first vouchers</li>"
        "</ol>"
        "<p>Change your password on first login.</p>"
    ),
    body_text=(
        "Welcome to {platform_name}, {contact_name}!\n\n"
        "Your account for {isp_name} has been approved.\n\n"
        "Login URL: {login_url}\nEmail: {admin_email}\nTemp password: {temp_password}\n\n"
        "You have a {trial_days}-day free trial starting now.\nChange your password on first login."
    ),
)
_default(
    "application_approved", SMS,
    body_text=(
        "Welcome to {platform_name}! Your ISP account is approved. "
        "Login: {login_url} | Email: {admin_email} | Temp password: {temp_password} | "
        "{trial_days}-day free trial starts now."
    ),
)

_default(
    "application_rejected", EMAIL,
    subject="Your application to {platform_name}",
    body_html=(
        "<h2>Hi {contact_name},</h2>"
        "<p>Thank you for your interest in <strong>{platform_name}</strong>.</p>"
        "<p>Unfortunately, we are unable to approve your application for <strong>{isp_name}</strong> at this time.</p>"
        "<p><strong>Reason:</strong> {rejection_reason}</p>"
        "<p>You are welcome to reapply if your circumstances change. "
        "If you have questions, contact us at <a href='mailto:{support_email}'>{support_email}</a>.</p>"
    ),
    body_text=(
        "Hi {contact_name},\n\nThank you for applying to {platform_name}.\n"
        "Unfortunately we cannot approve your application for {isp_name} at this time.\n"
        "Reason: {rejection_reason}\n\n"
        "You are welcome to reapply if circumstances change. Questions? {support_email}"
    ),
)
_default(
    "application_rejected", SMS,
    body_text=(
        "Hi {contact_name}, unfortunately we cannot approve your {platform_name} application "
        "at this time. Check your email for details."
    ),
)

_default(
    "trial_expiry_warning", EMAIL,
    subject="Your free trial ends in {days_remaining} days — {isp_name}",
    body_html=(
        "<h2>Your trial ends soon, {isp_name}</h2>"
        "<p>Your <strong>{platform_name}</strong> free trial ends on <strong>{trial_end_date}</strong> "
        "({days_remaining} days remaining).</p>"
        "<p>Monthly fee: <strong>GHS {monthly_fee_ghs}</strong></p>"
        "<p>After the trial, your account will switch to read-only mode until you pay your first invoice.</p>"
        "<p><a href='{billing_url}' style='background:#2563eb;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px'>Pay Now</a></p>"
    ),
    body_text=(
        "Your {platform_name} trial for {isp_name} ends on {trial_end_date} ({days_remaining} days remaining).\n"
        "Monthly fee: GHS {monthly_fee_ghs}\n"
        "Pay at: {billing_url}\n"
        "After the trial, your account enters read-only mode until payment."
    ),
)
_default(
    "trial_expiry_warning", SMS,
    body_text=(
        "Your {platform_name} trial ends in {days_remaining} days. "
        "Monthly fee: GHS {monthly_fee_ghs}. Pay at: {billing_url} to keep full access."
    ),
)

_default(
    "trial_expired", EMAIL,
    subject="Your trial has ended — action required",
    body_html=(
        "<h2>Your trial has ended, {isp_name}</h2>"
        "<p>Your <strong>{platform_name}</strong> free trial has ended. "
        "Your account is now in <strong>read-only mode</strong>.</p>"
        "<p>Pay your invoice to restore full access:</p>"
        "<p><a href='{billing_url}' style='background:#2563eb;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px'>Pay Invoice</a></p>"
    ),
    body_text=(
        "Your {platform_name} trial for {isp_name} has ended.\n"
        "Your account is now in read-only mode.\n"
        "Pay your invoice to restore access: {billing_url}"
    ),
)
_default(
    "trial_expired", SMS,
    body_text=(
        "Your {platform_name} trial has ended. Account is now read-only. "
        "Pay your invoice to restore access: {billing_url}"
    ),
)

_default(
    "invoice_issued", EMAIL,
    subject="Invoice {invoice_number} — GHS {amount_ghs} due {due_date}",
    body_html=(
        "<h2>Invoice {invoice_number}</h2>"
        "<table style='width:100%;border-collapse:collapse'>"
        "<tr><td>Operator:</td><td><strong>{isp_name}</strong></td></tr>"
        "<tr><td>Period:</td><td>{period_start} – {period_end}</td></tr>"
        "<tr><td>Amount:</td><td><strong>GHS {amount_ghs}</strong></td></tr>"
        "<tr><td>Due:</td><td><strong>{due_date}</strong></td></tr>"
        "</table>"
        "<br><a href='{payment_url}' style='background:#2563eb;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px'>Pay Now</a>"
        "<p>If payment is not received by {due_date}, a grace period will begin. "
        "Accounts unpaid after the grace period will be suspended.</p>"
    ),
    body_text=(
        "Invoice {invoice_number} for {isp_name}\n"
        "Period: {period_start} – {period_end}\n"
        "Amount: GHS {amount_ghs}\n"
        "Due: {due_date}\n"
        "Pay at: {payment_url}"
    ),
)
_default(
    "invoice_issued", SMS,
    body_text="Invoice {invoice_number} for GHS {amount_ghs} due {due_date}. Pay: {payment_url}",
)

_default(
    "grace_period", EMAIL,
    subject="Payment overdue — 14 days until suspension",
    body_html=(
        "<h2>Invoice overdue — {isp_name}</h2>"
        "<p>Invoice <strong>{invoice_number}</strong> for GHS <strong>{amount_ghs}</strong> is overdue.</p>"
        "<p>Your account will be <strong>suspended on {suspension_date}</strong> if payment is not received.</p>"
        "<p><a href='{payment_url}' style='background:#dc2626;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px'>Pay Now to Avoid Suspension</a></p>"
    ),
    body_text=(
        "Invoice {invoice_number} for {isp_name} is overdue.\n"
        "Amount: GHS {amount_ghs}\n"
        "Account suspended on: {suspension_date}\n"
        "Pay at: {payment_url}"
    ),
)
_default(
    "grace_period", SMS,
    body_text=(
        "{platform_name}: Invoice {invoice_number} overdue. "
        "Pay GHS {amount_ghs} by {suspension_date} to avoid suspension: {payment_url}"
    ),
)

_default(
    "account_suspended", EMAIL,
    subject="Account suspended — {isp_name}",
    body_html=(
        "<h2>Account suspended — {isp_name}</h2>"
        "<p>Your <strong>{platform_name}</strong> account has been suspended due to non-payment.</p>"
        "<p><strong>What this means:</strong></p>"
        "<ul><li>Existing active vouchers and sessions continue working</li>"
        "<li>No new vouchers can be generated</li>"
        "<li>No new payments via captive portal</li></ul>"
        "<p>Pay your outstanding invoice to restore full access:</p>"
        "<p><a href='{payment_url}' style='background:#2563eb;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px'>Pay to Reactivate</a></p>"
    ),
    body_text=(
        "Your {platform_name} account for {isp_name} has been suspended.\n"
        "Existing sessions continue but no new vouchers or payments.\n"
        "Pay to restore access: {payment_url}"
    ),
)
_default(
    "account_suspended", SMS,
    body_text=(
        "{platform_name}: Account suspended. "
        "Existing sessions continue but no new vouchers/payments. Pay to restore: {payment_url}"
    ),
)

_default(
    "account_reactivated", EMAIL,
    subject="Account reactivated — welcome back",
    body_html=(
        "<h2>Welcome back, {isp_name}!</h2>"
        "<p>Your payment has been confirmed and full access to <strong>{platform_name}</strong> has been restored.</p>"
        "<p>Next invoice date: <strong>{next_invoice_date}</strong></p>"
    ),
    body_text=(
        "Welcome back, {isp_name}!\n"
        "Payment confirmed. Full access to {platform_name} restored.\n"
        "Next invoice date: {next_invoice_date}"
    ),
)
_default(
    "account_reactivated", SMS,
    body_text="{platform_name}: Payment received! Full access restored. Next invoice: {next_invoice_date}.",
)


# ── render ────────────────────────────────────────────────────────────────

def _string_values(values: dict) -> dict[str, str]:
    """Every value as a plain string — Decimals and ints arrive from callers."""
    return {key: "" if value is None else str(value) for key, value in values.items()}


def render_sms(body_text: str, values: dict, *, fallback: str | None = None) -> str:
    return safe_format(body_text, _string_values(values), fallback=fallback)


def render_email(
    *, subject: str, body_text: str, body_html: str, values: dict, fallback: TemplateDef | None = None
) -> tuple[str, str, str]:
    """Returns (subject, full html document, plain text).

    HTML substitutes escaped values so a value carrying ``<`` or ``&`` renders
    as text instead of markup; the template's own tags are left alone.
    """
    plain = _string_values(values)
    escaped = escape_values(plain)
    return (
        safe_format(subject, plain, fallback=fallback.subject if fallback else None),
        wrap_html(safe_format(body_html, escaped, fallback=fallback.body_html if fallback else None)),
        safe_format(body_text, plain, fallback=fallback.body_text if fallback else None),
    )


# ── preview ───────────────────────────────────────────────────────────────

# Deliberately long-ish, realistic values: the preview doubles as the segment
# check, and a preview built from short samples would pass text that a real
# send splits across segments.
SAMPLE_VALUES: dict[str, str] = {
    "platform_name": DEFAULT_PLATFORM_NAME,
    "support_email": "support@yourplatform.com",
    "contact_name": "Kwame Mensah",
    "isp_name": "Aflao Community Networks",
    "login_url": "https://ip-admin.duckdns.org/admin",
    "admin_email": "admin@aflao-community.com",
    "temp_password": "Xk7mQ2pR9wLt",
    "trial_days": "14",
    "rejection_reason": "The coverage area overlaps an existing operator.",
    "trial_end_date": "30 September 2026",
    "days_remaining": "3",
    "monthly_fee_ghs": "250.00",
    "billing_url": "https://ip-admin.duckdns.org/admin/billing",
    "invoice_number": "INV-2026-0042",
    "amount_ghs": "250.00",
    "period_start": "1 September 2026",
    "period_end": "30 September 2026",
    "due_date": "7 October 2026",
    "payment_url": "https://ip-admin.duckdns.org/admin/billing",
    "suspension_date": "21 October 2026",
    "next_invoice_date": "1 November 2026",
}


def sample_values(event: str) -> dict[str, str]:
    """Only the placeholders this event actually offers."""
    definition = EVENTS[event]
    return {name: SAMPLE_VALUES[name] for name in definition.placeholders}


def preview(event: str, channel: str, *, subject: str | None, body_text: str, body_html: str | None) -> dict:
    """Render the sample values and, for SMS, measure the real segment count."""
    values = sample_values(event)
    if channel == SMS:
        text = render_sms(body_text, values)
        info = count_sms_segments(text)
        return {
            "channel": SMS,
            "body_text": text,
            "character_count": len(text),
            "encoding": info.encoding,
            "segment_count": info.segment_count,
        }
    rendered_subject, rendered_html, rendered_text = render_email(
        subject=subject or "", body_text=body_text, body_html=body_html or "", values=values
    )
    return {
        "channel": EMAIL,
        "subject": rendered_subject,
        "body_html": rendered_html,
        "body_text": rendered_text,
        "character_count": len(rendered_text),
    }


# ── validation ────────────────────────────────────────────────────────────

def _placeholder_error(text: str, definition: EventDef, where: str) -> str | None:
    if brace_error(text) or field_access_error(text):
        return f"Unbalanced or malformed {{ }} in the {where}."
    unknown = sorted({name for name in placeholders_in(text) if name not in definition.placeholders})
    if unknown:
        available = ", ".join("{" + name + "}" for name in definition.placeholders)
        return (
            f"Unknown placeholder(s) in the {where}: "
            + ", ".join("{" + name + "}" for name in unknown)
            + f". Available for this notification: {available}."
        )
    return None


def validation_error(
    event: str, channel: str, *, subject: str | None, body_text: str, body_html: str | None
) -> str | None:
    """None when the edit is safe to store, else the message to show."""
    if event not in EVENTS:
        return f"Unknown notification: {event}."
    if channel not in CHANNELS:
        return f"Unknown channel: {channel}."
    definition = EVENTS[event]

    if not isinstance(body_text, str) or not body_text.strip():
        return "The message cannot be empty."
    if len(body_text) > MAX_BODY_CHARS:
        return f"Keep the message under {MAX_BODY_CHARS} characters."

    parts: list[tuple[str, str]] = [(body_text, "message")]
    if channel == EMAIL:
        if not isinstance(subject, str) or not subject.strip():
            return "The subject cannot be empty."
        if len(subject) > MAX_SUBJECT_CHARS:
            return f"Keep the subject under {MAX_SUBJECT_CHARS} characters."
        if not isinstance(body_html, str) or not body_html.strip():
            return "The HTML body cannot be empty."
        if len(body_html) > MAX_BODY_CHARS:
            return f"Keep the HTML body under {MAX_BODY_CHARS} characters."
        parts += [(subject, "subject"), (body_html, "HTML body")]

    for text, where in parts:
        error = _placeholder_error(text, definition, where)
        if error:
            return error

    # A required placeholder must survive every edit: it carries something the
    # recipient cannot get anywhere else.
    for name in definition.required:
        needed = "{" + name + "}"
        if any(needed not in text for text, _ in parts if _ != "subject"):
            return (
                f"{needed} must appear in the message"
                + (" and the HTML body" if channel == EMAIL else "")
                + f" — {PLACEHOLDER_HELP[name].lower()}, and it is not recoverable afterwards."
            )

    if channel == SMS:
        measured = preview(event, SMS, subject=None, body_text=body_text, body_html=None)
        if measured["segment_count"] > MAX_SMS_SEGMENTS:
            limit = 160 if measured["encoding"] == "gsm7" else 70
            return (
                f"Too long: with realistic values this becomes {measured['character_count']} characters "
                f"({measured['segment_count']} SMS segments, {limit} characters each). "
                f"The limit is {MAX_SMS_SEGMENTS} segments."
            )
    return None
