"""Unit tests for the platform notification templates (catalog, validation, render).

The headline test is test_every_default_renders_exactly_what_we_sent_before:
migration 044 seeds this catalog's text, so if a default drifts from what
notifications/email/templates.py produces, operators quietly start receiving
different emails. That module is kept solely as the reference implementation
for this comparison — nothing in production calls it any more.

No server and no database. Safe to run anywhere, including the production
container. The HTTP endpoints are covered by test_notification_templates_flow.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.modules.notifications import template_catalog as catalog
from src.modules.notifications.email import templates as legacy
from src.modules.notifications.template_catalog import EMAIL, SMS

APPROVED = "application_approved"


# ── the catalog is internally consistent ──────────────────────────────────

def test_every_event_has_both_channels():
    assert set(catalog.DEFAULTS) == {(event, channel) for event in catalog.EVENTS for channel in catalog.CHANNELS}


def test_every_declared_placeholder_has_help_text_and_a_sample():
    for definition in catalog.EVENTS.values():
        for name in definition.placeholders:
            assert name in catalog.PLACEHOLDER_HELP, f"{definition.event}: {name} has no help text"
            assert name in catalog.SAMPLE_VALUES, f"{definition.event}: {name} has no preview sample"
        for name in definition.required:
            assert name in definition.placeholders


def test_every_default_uses_only_placeholders_its_event_declares():
    from src.utils.templating import placeholders_in

    for (event, channel), default in catalog.DEFAULTS.items():
        allowed = set(catalog.EVENTS[event].placeholders)
        for text in filter(None, (default.subject, default.body_text, default.body_html)):
            assert set(placeholders_in(text)) <= allowed, f"{event}/{channel} uses an undeclared placeholder"


def test_every_default_passes_its_own_validation():
    for (event, channel), default in catalog.DEFAULTS.items():
        error = catalog.validation_error(
            event, channel, subject=default.subject, body_text=default.body_text, body_html=default.body_html
        )
        assert error is None, f"{event}/{channel}: {error}"


def test_sms_defaults_are_sms_shaped_and_email_defaults_are_not():
    for (event, channel), default in catalog.DEFAULTS.items():
        if channel == SMS:
            assert default.subject is None and default.body_html is None
        else:
            assert default.subject and default.body_html


# ── the defaults are what we used to send ─────────────────────────────────

LEGACY_CASES = {
    "application_received": (
        legacy.application_received,
        dict(contact_name="Kwame Mensah", isp_name="Aflao Community Networks", support_email="support@x.com"),
    ),
    "application_approved": (
        legacy.application_approved,
        dict(contact_name="Kwame Mensah", isp_name="Aflao Community Networks",
             login_url="https://ip-admin.example/admin", admin_email="admin@aflao.com",
             temp_password="Xk7mQ2pR9wLt", trial_days=14),
    ),
    "application_rejected": (
        legacy.application_rejected,
        dict(contact_name="Kwame Mensah", isp_name="Aflao Community Networks",
             rejection_reason="Coverage overlaps an existing operator.", support_email="support@x.com"),
    ),
    "trial_expiry_warning": (
        legacy.trial_expiry_warning,
        dict(isp_name="Aflao", trial_end_date="30 September 2026", days_remaining=3,
             monthly_fee_ghs=Decimal("250.00"), billing_url="https://x/billing"),
    ),
    "trial_expired": (legacy.trial_expired, dict(isp_name="Aflao", billing_url="https://x/billing")),
    "invoice_issued": (
        legacy.invoice_issued,
        dict(isp_name="Aflao", invoice_number="INV-2026-0042", amount_ghs=Decimal("250.00"),
             period_start="1 September 2026", period_end="30 September 2026",
             due_date="7 October 2026", payment_url="https://x/pay"),
    ),
    "grace_period": (
        legacy.grace_period_warning,
        dict(isp_name="Aflao", invoice_number="INV-2026-0042", amount_ghs=Decimal("250.00"),
             suspension_date="21 October 2026", payment_url="https://x/pay"),
    ),
    "account_suspended": (legacy.account_suspended, dict(isp_name="Aflao", payment_url="https://x/pay")),
    "account_reactivated": (legacy.account_reactivated, dict(isp_name="Aflao", next_invoice_date="1 November 2026")),
}


def test_legacy_cases_cover_every_event():
    assert set(LEGACY_CASES) == set(catalog.EVENTS)


@pytest.mark.parametrize("event", sorted(LEGACY_CASES))
def test_every_default_renders_exactly_what_we_sent_before(event):
    """Character-identical to the f-string version, subject, HTML and text."""
    function, values = LEGACY_CASES[event]
    expected_subject, expected_html, expected_text = function(**values)

    default = catalog.DEFAULTS[(event, EMAIL)]
    subject, html, text = catalog.render_email(
        subject=default.subject,
        body_text=default.body_text,
        body_html=default.body_html,
        values={**values, "platform_name": catalog.DEFAULT_PLATFORM_NAME},
    )
    assert subject == expected_subject
    assert html == expected_html
    assert text == expected_text


# ── validation ────────────────────────────────────────────────────────────

def _approval_sms(body: str) -> str | None:
    return catalog.validation_error(APPROVED, SMS, subject=None, body_text=body, body_html=None)


def test_required_temp_password_cannot_be_edited_out_of_the_approval_sms():
    error = _approval_sms("Your account is approved. Sign in at {login_url} as {admin_email}.")
    assert error is not None and "{temp_password}" in error


def test_required_temp_password_must_survive_in_both_email_parts():
    default = catalog.DEFAULTS[(APPROVED, EMAIL)]
    stripped_html = default.body_html.replace("{temp_password}", "(ask support)")
    error = catalog.validation_error(
        APPROVED, EMAIL, subject=default.subject, body_text=default.body_text, body_html=stripped_html
    )
    assert error is not None and "{temp_password}" in error

    stripped_text = default.body_text.replace("{temp_password}", "(ask support)")
    error = catalog.validation_error(
        APPROVED, EMAIL, subject=default.subject, body_text=stripped_text, body_html=default.body_html
    )
    assert error is not None and "{temp_password}" in error


def test_the_subject_is_not_required_to_carry_the_temp_password():
    """It would land the one-time password in mail-server logs and notifications."""
    default = catalog.DEFAULTS[(APPROVED, EMAIL)]
    assert "{temp_password}" not in default.subject
    assert catalog.validation_error(
        APPROVED, EMAIL, subject=default.subject, body_text=default.body_text, body_html=default.body_html
    ) is None


def test_unknown_placeholder_is_named_and_the_available_ones_listed():
    error = _approval_sms("Code {temp_password} for {voucher_code}.")
    assert "{voucher_code}" in error
    assert "{temp_password}" in error  # listed as available


def test_a_placeholder_from_a_different_event_is_still_unknown():
    error = catalog.validation_error(
        "application_received", SMS,
        subject=None, body_text="Hi {contact_name}, invoice {invoice_number}.", body_html=None,
    )
    assert error is not None and "{invoice_number}" in error


@pytest.mark.parametrize("body", [
    "Password {temp_password} and a stray {",
    "Password {temp_password} }",
    "Password {temp_password} {{unbalanced}",
])
def test_malformed_braces_are_rejected(body):
    assert "malformed" in (_approval_sms(body) or "")


@pytest.mark.parametrize("body", [
    "Password {temp_password.__class__}",
    "Password {temp_password[0]}",
    "Password {temp_password!r}",
    "Password {0}",
])
def test_a_placeholder_may_only_name_a_key(body):
    """Attribute and index access are legal str.format syntax — refused here."""
    assert _approval_sms(body) is not None


@pytest.mark.parametrize("body", ["", "   ", "\n"])
def test_empty_message_is_rejected(body):
    assert "empty" in (_approval_sms(body) or "")


def test_email_needs_a_subject_and_an_html_body():
    default = catalog.DEFAULTS[(APPROVED, EMAIL)]
    assert "subject" in catalog.validation_error(
        APPROVED, EMAIL, subject="  ", body_text=default.body_text, body_html=default.body_html)
    assert "HTML" in catalog.validation_error(
        APPROVED, EMAIL, subject=default.subject, body_text=default.body_text, body_html="")


def test_over_long_bodies_are_rejected():
    assert "characters" in _approval_sms("{temp_password} " + "x" * catalog.MAX_BODY_CHARS)


def test_unknown_event_or_channel_is_rejected():
    assert catalog.validation_error("no_such_event", SMS, subject=None, body_text="{x}", body_html=None)
    assert catalog.validation_error(APPROVED, "carrier_pigeon", subject=None, body_text="hi", body_html=None)


# ── segment counting ──────────────────────────────────────────────────────

def test_segment_cap_is_enforced_with_the_real_segmentation_code():
    body = "{temp_password} " + "x" * 700
    error = _approval_sms(body)
    assert error is not None
    assert f"{catalog.MAX_SMS_SEGMENTS} segments" in error
    assert "160 characters each" in error


def test_a_message_just_inside_the_cap_is_accepted():
    from src.modules.sms.segmentation import count_sms_segments

    body = "{temp_password} " + "x" * 400
    rendered = catalog.render_sms(body, catalog.sample_values(APPROVED))
    assert count_sms_segments(rendered).segment_count == catalog.MAX_SMS_SEGMENTS
    assert _approval_sms(body) is None


def test_non_gsm_characters_switch_the_preview_to_ucs2_and_the_quoted_limit():
    body = "{temp_password} ☎ " + "x" * 200
    measured = catalog.preview(APPROVED, SMS, subject=None, body_text=body, body_html=None)
    assert measured["encoding"] == "ucs2"
    assert "70 characters each" in _approval_sms("{temp_password} ☎ " + "x" * 700)


def test_preview_measures_the_rendered_text_not_the_template():
    short_template = "{temp_password} {login_url}"
    measured = catalog.preview(APPROVED, SMS, subject=None, body_text=short_template, body_html=None)
    assert measured["character_count"] > len(short_template)
    assert "{" not in measured["body_text"]


def test_preview_only_offers_the_placeholders_the_event_declares():
    for event, definition in catalog.EVENTS.items():
        assert set(catalog.sample_values(event)) == set(definition.placeholders)


# ── rendering ─────────────────────────────────────────────────────────────

def test_html_substitution_escapes_values_but_not_the_template_markup():
    values = {
        "contact_name": "Kwame",
        "isp_name": "A & B <Networks>",
        "rejection_reason": "<script>alert(1)</script>",
        "support_email": "support@x.com",
        "platform_name": catalog.DEFAULT_PLATFORM_NAME,
    }
    default = catalog.DEFAULTS[("application_rejected", EMAIL)]
    _, html, text = catalog.render_email(
        subject=default.subject, body_text=default.body_text, body_html=default.body_html, values=values
    )
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "A &amp; B &lt;Networks&gt;" in html
    assert "<strong>" in html  # the template's own markup is untouched
    # The plain-text part is not HTML, so it is not escaped.
    assert "<script>alert(1)</script>" in text


def test_a_stored_template_that_is_malformed_falls_back_to_the_default():
    """A row can be changed outside the endpoint; a send must still go out."""
    default = catalog.DEFAULTS[(APPROVED, SMS)]
    rendered = catalog.render_sms(
        "Broken {temp_password", catalog.sample_values(APPROVED), fallback=default.body_text
    )
    assert catalog.SAMPLE_VALUES["temp_password"] in rendered
    assert "Broken" not in rendered


def test_a_retired_placeholder_renders_visibly_instead_of_raising():
    rendered = catalog.render_sms("Code {temp_password} ref {retired_thing}", catalog.sample_values(APPROVED))
    assert "[retired_thing?]" in rendered


def test_values_are_stringified_and_none_becomes_empty():
    rendered = catalog.render_sms(
        "Invoice {invoice_number} GHS {amount_ghs} due {due_date}",
        {"invoice_number": None, "amount_ghs": Decimal("250.00"), "due_date": 7},
    )
    assert rendered == "Invoice  GHS 250.00 due 7"


def test_email_render_always_returns_a_wrapped_document():
    default = catalog.DEFAULTS[("account_reactivated", EMAIL)]
    _, html, _ = catalog.render_email(
        subject=default.subject, body_text=default.body_text, body_html=default.body_html,
        values=catalog.sample_values("account_reactivated"),
    )
    assert html.startswith("<html><body") and html.endswith("</body></html>")
