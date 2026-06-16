"""Captive-portal branding defaults and resolution.

The default colours are the values currently hardcoded in the portal CSS/markup
(``portal/statics/style.css`` and ``portal/login.html``), so an operator that has
configured nothing renders byte-for-byte the same portal as before.

  - primary  #2563eb  -> .btn-primary background, input focus border, plan accents,
                         and the login logo SVG fill (#2563EB)
  - accent       #764ba2  -> the end stop of the page background gradient
  - gradient start #667eea -> the start stop of the page background gradient
                         (linear-gradient 135deg, #667eea 0%, #764ba2 100%)
  - welcome               -> the login page subtitle text
"""
from __future__ import annotations

from typing import Any

from src.schemas import BrandingResponse

DEFAULT_PRIMARY_COLOR = "#2563eb"
DEFAULT_ACCENT_COLOR = "#764ba2"
DEFAULT_BACKGROUND_GRADIENT_START = "#667eea"
DEFAULT_WELCOME_MESSAGE = "Enter your voucher code to get online"


def build_branding(operator: Any | None) -> BrandingResponse:
    """Return branding for an operator (or platform-wide defaults when ``None``),
    filling every unset field with its default so callers never see a null colour
    or message. ``operator`` is any object exposing the branding attributes (an
    ``ISPOperator`` row, or a stub in tests)."""
    def _get(attr: str) -> Any:
        return getattr(operator, attr, None) if operator is not None else None

    display_name = _get("portal_display_name") or _get("name") or None
    return BrandingResponse(
        portal_display_name=display_name,
        logo_url=_get("logo_url"),
        primary_color=_get("primary_color") or DEFAULT_PRIMARY_COLOR,
        accent_color=_get("accent_color") or DEFAULT_ACCENT_COLOR,
        background_gradient_start=_get("background_gradient_start") or DEFAULT_BACKGROUND_GRADIENT_START,
        welcome_message=_get("portal_welcome_message") or DEFAULT_WELCOME_MESSAGE,
    )
