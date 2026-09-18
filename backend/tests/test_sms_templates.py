"""Unit tests for the operator purchase-SMS template: safe substitution,
placeholder allowlist and the one-segment limit.

No server and no database. Safe to run anywhere, including production.
"""
from __future__ import annotations

import pytest

from src.modules.sms import templates as t
from src.modules.sms.segmentation import count_sms_segments


def render_sample(template: str) -> str:
    return t.render(template, code="ABCD-EFGH-IJKL-MNOP", plan_name="Quick pass",
                    validity="2 hrs", operator_name="Aflao-net")


# ── substitution safety ───────────────────────────────────────────────────


def test_placeholders_substitute():
    out = render_sample("{operator}: {code} — {plan}, {validity}")
    assert out == "Aflao-net: ABCD-EFGH-IJKL-MNOP — Quick pass, 2 hrs"


@pytest.mark.parametrize("template", [
    "{code.__class__}",          # attribute access
    "{code[0]}",                 # index access
    "{0}",                       # positional
    "{}",                        # auto-numbering
    "{code!r}",                  # conversion
    "{code:>200}",               # format spec padding to another segment
])
def test_format_tricks_never_execute_or_raise(template):
    """str.format would allow attribute/index access; the mapping must not."""
    out = render_sample(template)
    assert "__class__" not in out and "builtins" not in out
    assert isinstance(out, str) and out


def test_unknown_placeholder_degrades_visibly_instead_of_raising():
    assert render_sample("Hi {nope}") == "Hi [nope?]"


@pytest.mark.parametrize("template", ["{", "}", "a { b", "{code} }"])
def test_malformed_braces_fall_back_to_the_default_text(template):
    out = render_sample(template)
    assert out.startswith("Your WiFi voucher:")


def test_long_values_are_clipped_at_render_time():
    out = t.render("{plan}|{operator}", code="C", plan_name="P" * 80,
                   validity="2 hrs", operator_name="O" * 80)
    plan_part, operator_part = out.split("|")
    assert len(plan_part) == t.PLAN_NAME_MAX and plan_part.endswith("…")
    assert len(operator_part) == t.OPERATOR_NAME_MAX and operator_part.endswith("…")


# ── validation ────────────────────────────────────────────────────────────


def test_the_default_template_is_valid_and_one_segment():
    assert t.validation_error(t.DEFAULT_VOUCHER_SMS_TEMPLATE) is None
    assert t.preview(t.DEFAULT_VOUCHER_SMS_TEMPLATE).segment_count == 1


@pytest.mark.parametrize("template,fragment", [
    ("", "cannot be empty"),
    ("   ", "cannot be empty"),
    ("Your code is {voucher}", "Unknown placeholder"),
    ("Your code is {expiry}", "Unknown placeholder"),
    ("Plan {plan} only", "must include {code}"),
    ("x" * (t.MAX_TEMPLATE_CHARS + 1), "under"),
])
def test_rejected_templates(template, fragment):
    error = t.validation_error(template)
    assert error and fragment in error


def test_boundary_exactly_one_segment_is_accepted_and_one_more_is_not():
    """Padding is sized so the rendered worst case lands exactly on 160."""
    fixed = t.render("{code}", code=t.SAMPLE_CODE, plan_name="", validity="", operator_name="")
    padding = 160 - len(fixed)
    exactly = "{code}" + ("x" * padding)
    assert t.preview(exactly).character_count == 160
    assert t.preview(exactly).segment_count == 1
    assert t.validation_error(exactly) is None

    one_over = exactly + "x"
    assert t.preview(one_over).character_count == 161
    assert t.preview(one_over).segment_count == 2
    error = t.validation_error(one_over)
    assert error and "161 characters" in error and "160-character" in error


def test_non_gsm_characters_drop_the_limit_to_70():
    """A non-GSM character forces UCS-2, where one segment is 70 units.

    Uses a BMP character (one UTF-16 unit): an emoji is a surrogate pair, so 70
    of those characters are 71 units and would legitimately need two segments.
    """
    fixed = t.render("{code}", code=t.SAMPLE_CODE, plan_name="", validity="", operator_name="")
    exactly_70 = "Д{code}" + ("x" * (70 - len(fixed) - 1))
    measured = t.preview(exactly_70)
    assert measured.encoding == "ucs2" and measured.character_count == 70
    assert measured.segment_count == 1 and t.validation_error(exactly_70) is None

    one_over = exactly_70 + "x"
    error = t.validation_error(one_over)
    assert error and "70-character" in error and "Non-GSM" in error


def test_validation_uses_the_worst_case_not_the_short_sample():
    """A template that fits with a short plan name but not with the longest one."""
    fixed = t.render("{code}", code=t.SAMPLE_CODE, plan_name="", validity="", operator_name="")
    # Fills the segment once {plan} renders at its maximum length.
    template = "{code}{plan}" + ("x" * (160 - len(fixed) - t.PLAN_NAME_MAX + 1))
    assert count_sms_segments(t.render(template, code=t.SAMPLE_CODE, plan_name="Wifi",
                                       validity="", operator_name="")).segment_count == 1
    assert t.validation_error(template) is not None


def test_preview_reports_encoding_and_counts():
    measured = t.preview("{code} ok")
    assert measured.encoding == "gsm7" and measured.segment_count == 1
    assert measured.text.startswith(t.SAMPLE_CODE)
    assert measured.character_count == len(measured.text)


def test_placeholders_in_lists_names_in_order():
    assert t.placeholders_in("{operator} {code} {plan}") == ["operator", "code", "plan"]
    assert t.placeholders_in("no placeholders") == []
