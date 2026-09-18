"""Safe placeholder substitution for operator- and platform-editable message text.

Stored template text is user input, so it is never f-string-evaluated and never
passed to str.format — str.format would expose attribute and index access
(``{code.__class__}``, ``{code[0]}``) on whatever objects it is handed. Every
substitution here goes through str.format_map with a Mapping that only ever
returns plain strings, so those forms have nothing to reach into, and unknown
keys degrade to a visible marker instead of raising: a saved template can
outlive a placeholder we retire.

Shared by the operator voucher SMS (modules/sms/templates.py) and the platform
notification templates (modules/notifications/template_catalog.py).
"""
from __future__ import annotations

import html as html_module
import re

__all__ = [
    "SafeTemplateValues",
    "placeholders_in",
    "brace_error",
    "field_access_error",
    "safe_format",
    "escape_values",
]

_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")


class SafeTemplateValues(dict):
    """Mapping for str.format_map: unknown keys render visibly instead of raising."""

    def __missing__(self, key: str) -> str:
        return f"[{key}?]"


def placeholders_in(template: str) -> list[str]:
    """Every {name} in the template, in order of appearance."""
    return [match.group(1).strip() for match in _PLACEHOLDER_RE.finditer(template or "")]


def brace_error(template: str) -> bool:
    """True when the text has braces that are not well-formed {name} pairs."""
    return (
        template.count("{") != template.count("}")
        or len(placeholders_in(template)) != template.count("{")
    )


def field_access_error(template: str) -> bool:
    """True when any placeholder does more than name a key.

    ``{code.__class__}`` and ``{code[0]}`` are legal str.format syntax and would
    otherwise render an attribute or element of the substituted value instead of
    raising. Nothing reachable that way is sensitive (the values are plain
    strings and format cannot call anything), but a placeholder is a name here
    and nothing else, so these are rejected outright rather than rendered.
    """
    return any(
        (not name) or ("." in name) or ("[" in name) or ("!" in name) or (":" in name) or name.isdigit()
        for name in placeholders_in(template)
    )


def safe_format(template: str, values: dict, *, fallback: str | None = None) -> str:
    """Substitute a stored template. Never raises on bad placeholders or braces.

    ``fallback`` is rendered instead when the stored text is malformed, so a bad
    saved template degrades to the shipped default rather than losing the send.
    """
    mapping = values if isinstance(values, SafeTemplateValues) else SafeTemplateValues(values)
    if field_access_error(template):
        template = fallback if fallback is not None else ""
    try:
        return template.format_map(mapping)
    except (IndexError, KeyError, ValueError, AttributeError, TypeError):
        if fallback is None:
            return template
        try:
            return fallback.format_map(mapping)
        except (IndexError, KeyError, ValueError, AttributeError, TypeError):
            return fallback


def escape_values(values: dict) -> SafeTemplateValues:
    """The same values, HTML-escaped, for substitution into an HTML body.

    Only the substituted values are escaped — the template's own markup is not,
    which is the point: a rejection reason or operator name containing ``&`` or
    ``<`` cannot break out into the surrounding HTML.
    """
    escaped = SafeTemplateValues(
        {key: html_module.escape(str(value), quote=True) for key, value in values.items()}
    )
    return escaped
