"""Shared construction of the per-operator payment-provider webhook URL.

One helper so the URL shape stays in one place, matching the explicit routes
registered in ``src.modules.webhooks.routes`` (one route per provider, e.g.
``/api/v1/webhooks/paystack/{operator_slug}``).
"""
from __future__ import annotations

from src.config import get_settings


def build_webhook_url(provider_key: str, slug: str | None) -> str | None:
    webhook_base = get_settings().webhook_base_url.rstrip("/")
    if not webhook_base or not slug:
        return None
    return f"{webhook_base}/api/v1/webhooks/{provider_key}/{slug}"
