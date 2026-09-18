"""Operator-editable purchase-confirmation SMS: placeholders, validation, render.

Stored template text is operator input, so it is never f-string-evaluated and
never passed to str.format. Substitution goes through utils.templating, which
is shared with the platform notification templates — see its module docstring
for why format_map with a safe Mapping is the only form used here.

Validation is length-first: an operator editing this text is spending their own
SMS credit (or the platform's, billed back per segment), so a template that
would push a real message past one segment is refused at save time rather than
silently doubling the cost of every voucher sold.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.modules.sms.segmentation import count_sms_segments
from src.utils.templating import brace_error, field_access_error, placeholders_in, safe_format

# The default every operator sends today (sms.service.build_voucher_sms_message).
DEFAULT_VOUCHER_SMS_TEMPLATE = (
    "Your WiFi voucher: {code}. Plan: {plan}. Valid for {validity}. "
    "Connect at the login page. Enjoy!"
)

# Placeholder -> what it renders, for the editor's help text.
PLACEHOLDERS: dict[str, str] = {
    "code": "The voucher code, e.g. ABCD-EFGH-IJKL-MNOP",
    "plan": "Plan name, shortened if very long",
    "validity": "What the plan gives, e.g. “2 hrs” or “500 MB”",
    "operator": "Your business name",
}

# Rendered values are bounded so validation can prove a real send fits: the plan
# and operator names are truncated to these lengths at render time.
PLAN_NAME_MAX = 24
OPERATOR_NAME_MAX = 20

# Worst case used for validation. The code sample is the DEFAULT 16-character
# code as printed (4 groups of 4 + 3 dashes = 19). A plan configured with a
# longer code length (up to 24 -> 29 printed) can still exceed one segment at
# send time; the editor says so rather than pretending otherwise.
VALIDATION_CODE_LENGTH = 16
SAMPLE_CODE = "ABCD-EFGH-IJKL-MNOP"
SAMPLE_PLAN = "W" * PLAN_NAME_MAX
SAMPLE_VALIDITY = "999 mins + 999.9 GB"  # longest shape _format_duration produces
SAMPLE_OPERATOR = "W" * OPERATOR_NAME_MAX

MAX_TEMPLATE_CHARS = 320  # a sane upper bound before segment checking

@dataclass(frozen=True)
class TemplatePreview:
    text: str
    encoding: str
    segment_count: int
    character_count: int


def render(template: str, *, code: str, plan_name: str, validity: str, operator_name: str) -> str:
    """Substitute a stored template. Never raises on a bad placeholder."""
    values = {
        "code": code,
        "plan": _clip(plan_name, PLAN_NAME_MAX),
        "validity": validity,
        "operator": _clip(operator_name, OPERATOR_NAME_MAX),
    }
    # Malformed braces or a non-name placeholder fall back to the default text;
    # a stored template must never break the send.
    return safe_format(template, values, fallback=DEFAULT_VOUCHER_SMS_TEMPLATE)


def preview(template: str) -> TemplatePreview:
    """Render the worst realistic case and measure it."""
    text = render(
        template,
        code=SAMPLE_CODE,
        plan_name=SAMPLE_PLAN,
        validity=SAMPLE_VALIDITY,
        operator_name=SAMPLE_OPERATOR,
    )
    info = count_sms_segments(text)
    return TemplatePreview(
        text=text,
        encoding=info.encoding,
        segment_count=info.segment_count,
        character_count=len(text),
    )


def validation_error(template: str) -> str | None:
    """None when the template is safe to store, else the message to show."""
    if not isinstance(template, str) or not template.strip():
        return "The message cannot be empty."
    if len(template) > MAX_TEMPLATE_CHARS:
        return f"Keep the message under {MAX_TEMPLATE_CHARS} characters."

    unknown = sorted({name for name in placeholders_in(template) if name not in PLACEHOLDERS})
    if unknown:
        known = ", ".join("{" + name + "}" for name in PLACEHOLDERS)
        return (
            "Unknown placeholder(s): "
            + ", ".join("{" + name + "}" for name in unknown)
            + f". Available: {known}."
        )
    # Braces that survive the placeholder scan are malformed ("{", "}", "{{a}"),
    # as is anything doing more than naming a key ("{code[0]}", "{code!r}").
    if brace_error(template) or field_access_error(template):
        return "Unbalanced or malformed { } in the message."

    if "{code}" not in template:
        return "The message must include {code} — otherwise the buyer never receives their voucher."

    measured = preview(template)
    if measured.segment_count > 1:
        limit = 160 if measured.encoding == "gsm7" else 70
        extra = (
            " Non-GSM characters (e.g. emoji or “smart” quotes) cut the limit to 70."
            if measured.encoding == "ucs2"
            else ""
        )
        return (
            f"Too long: with the longest plan name and voucher code this becomes "
            f"{measured.character_count} characters, over the {limit}-character single-SMS limit."
            + extra
        )
    return None


def _clip(value: str, limit: int) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[: max(0, limit - 1)].rstrip() + "…"
