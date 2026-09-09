"""Canonical provider catalog: the payment and SMS providers the platform knows about.

This module is the single source of truth for *which* providers exist and what
credentials each one needs.  Rows are created only by migration 021 or by
re-running the seed (`src/db/seeds/seed_provider_catalog.py`) — there is no
create/delete API.  The platform admin's only writes are the availability toggle
and, on platform-provided SMS entries, the per-message rate.

Two SMS delivery models are represented:

  * ``is_platform_provided=False`` — bring-your-own.  The operator supplies their
    own gateway credentials and is billed directly by that gateway.  The platform
    charges nothing.
  * ``is_platform_provided=True`` — the platform's own gateway.  The *platform
    admin's* credentials are used, and operators on this option are billed
    per-message on their monthly invoice at ``platform_rate_per_message``.
    ``credential_schema.configured_by`` says which side owns the credentials, so
    the operator-facing config UI knows not to ask the operator for keys here.

Re-seeding is idempotent and deliberately partial: display_name, description,
credential_schema, is_integrated, is_platform_provided and sort_order are
refreshed from this file, but ``is_available`` and ``platform_rate_per_message``
are never touched — those are the platform admin's choices and a redeploy must
not clobber them.
"""
from __future__ import annotations

import json

from sqlalchemy import text

# Credential-field descriptor shape, consumed by the later operator-config UI:
#   {"name", "label", "type", "required", "secret"}
# Wrapped in an object so the schema can also declare who configures it.
PROVIDER_CATALOG: list[dict] = [
    # ---------------- Payment ----------------
    {
        "category": "payment",
        "provider_key": "paystack",
        "display_name": "Paystack",
        "description": "Cards, bank transfer and mobile money. The default and only fully wired payment provider.",
        "is_integrated": True,
        "is_available": True,
        "is_platform_provided": False,
        "sort_order": 10,
        "credential_schema": {
            "configured_by": "operator",
            "fields": [
                {"name": "public_key", "label": "Public key", "type": "string", "required": True, "secret": False},
                {"name": "secret_key", "label": "Secret key", "type": "string", "required": True, "secret": True},
                {"name": "webhook_secret", "label": "Webhook secret", "type": "string", "required": False, "secret": True},
            ],
        },
    },
    {
        "category": "payment",
        "provider_key": "flutterwave",
        "display_name": "Flutterwave",
        "description": "Cards and mobile money via Flutterwave. Operator supplies their own Flutterwave keys.",
        "is_integrated": True,
        # Still off — the platform admin flips this on /platform/providers after
        # the live Flutterwave sandbox round-trip. Not touched by the catalog sync.
        "is_available": False,
        "is_platform_provided": False,
        "sort_order": 20,
        "credential_schema": {
            "configured_by": "operator",
            "fields": [
                {"name": "public_key", "label": "Public key (FLWPUBK-…)", "type": "string", "required": True, "secret": False},
                {"name": "secret_key", "label": "Secret key (FLWSECK-…)", "type": "string", "required": True, "secret": True},
                {"name": "webhook_secret", "label": "Webhook secret hash", "type": "string", "required": False, "secret": True},
            ],
        },
    },
    {
        "category": "payment",
        "provider_key": "mtn_momo",
        "display_name": "MTN Mobile Money",
        "description": "Not yet integrated.",
        "is_integrated": False,
        "is_available": False,
        "is_platform_provided": False,
        "sort_order": 30,
        "credential_schema": {"configured_by": "operator", "fields": []},
    },
    {
        "category": "payment",
        "provider_key": "vodafone_cash",
        "display_name": "Vodafone Cash",
        "description": "Not yet integrated.",
        "is_integrated": False,
        "is_available": False,
        "is_platform_provided": False,
        "sort_order": 40,
        "credential_schema": {"configured_by": "operator", "fields": []},
    },
    {
        "category": "payment",
        "provider_key": "airteltigo",
        "display_name": "AirtelTigo Money",
        "description": "Not yet integrated.",
        "is_integrated": False,
        "is_available": False,
        "is_platform_provided": False,
        "sort_order": 50,
        "credential_schema": {"configured_by": "operator", "fields": []},
    },

    # ---------------- SMS ----------------
    {
        "category": "sms",
        "provider_key": "africastalking_platform",
        "display_name": "Africa's Talking (platform gateway)",
        "description": (
            "Operators send through the platform's own Africa's Talking account and are "
            "billed per message on their monthly invoice. No operator credentials required."
        ),
        # The AT client exists, but the send-via-platform-gateway path (and the
        # storage for the platform's own credentials) lands after the operator
        # payment-credential work.  Marked not-integrated so the availability
        # toggle stays blocked until that path is real; the rate is settable now.
        "is_integrated": False,
        "is_available": False,
        "is_platform_provided": True,
        "sort_order": 10,
        "credential_schema": {
            # Platform admin's own AT account — never asked of an operator.
            "configured_by": "platform_admin",
            "fields": [
                {"name": "api_key", "label": "Africa's Talking API key", "type": "string", "required": True, "secret": True},
                {"name": "username", "label": "Africa's Talking username", "type": "string", "required": True, "secret": False},
                {"name": "sender_id", "label": "Sender ID", "type": "string", "required": True, "secret": False},
            ],
        },
    },
    {
        "category": "sms",
        "provider_key": "hubtel",
        "display_name": "Hubtel SMS",
        "description": "Operator supplies their own Hubtel credentials and is billed by Hubtel directly.",
        "is_integrated": True,
        "is_available": False,
        "is_platform_provided": False,
        "sort_order": 20,
        "credential_schema": {
            "configured_by": "operator",
            "fields": [
                {"name": "client_id", "label": "Client ID", "type": "string", "required": True, "secret": False},
                {"name": "client_secret", "label": "Client secret", "type": "string", "required": True, "secret": True},
                {"name": "from", "label": "Sender ID", "type": "string", "required": True, "secret": False},
            ],
        },
    },
    {
        "category": "sms",
        "provider_key": "africastalking",
        "display_name": "Africa's Talking (own account)",
        "description": "Operator supplies their own Africa's Talking credentials and is billed by AT directly.",
        "is_integrated": True,
        "is_available": False,
        "is_platform_provided": False,
        "sort_order": 30,
        "credential_schema": {
            "configured_by": "operator",
            "fields": [
                {"name": "api_key", "label": "API key", "type": "string", "required": True, "secret": True},
                {"name": "username", "label": "Username", "type": "string", "required": True, "secret": False},
                {"name": "from", "label": "Sender ID", "type": "string", "required": True, "secret": False},
            ],
        },
    },
]


# is_available and platform_rate_per_message are intentionally absent from the
# UPDATE clause: they belong to the platform admin, not to this file.
_UPSERT_SQL = text(
    """
    INSERT INTO provider_catalog (
        category, provider_key, display_name, description,
        credential_schema, is_integrated, is_available,
        is_platform_provided, sort_order
    ) VALUES (
        CAST(:category AS provider_category), :provider_key, :display_name, :description,
        CAST(:credential_schema AS JSONB), :is_integrated, :is_available,
        :is_platform_provided, :sort_order
    )
    ON CONFLICT (category, provider_key) DO UPDATE SET
        display_name        = EXCLUDED.display_name,
        description         = EXCLUDED.description,
        credential_schema   = EXCLUDED.credential_schema,
        is_integrated       = EXCLUDED.is_integrated,
        is_platform_provided = EXCLUDED.is_platform_provided,
        sort_order          = EXCLUDED.sort_order,
        updated_at          = NOW()
    """
)


def _params(entry: dict) -> dict:
    return {
        "category": entry["category"],
        "provider_key": entry["provider_key"],
        "display_name": entry["display_name"],
        "description": entry.get("description"),
        "credential_schema": json.dumps(entry.get("credential_schema") or {}),
        "is_integrated": entry["is_integrated"],
        "is_available": entry["is_available"],
        "is_platform_provided": entry["is_platform_provided"],
        "sort_order": entry["sort_order"],
    }


def sync_provider_catalog(bind) -> int:
    """Upsert every catalog entry over a synchronous Connection (Alembic). Caller commits."""
    for entry in PROVIDER_CATALOG:
        bind.execute(_UPSERT_SQL, _params(entry))
    return len(PROVIDER_CATALOG)


async def async_sync_provider_catalog(conn) -> int:
    """Upsert every catalog entry over an AsyncConnection/AsyncSession. Caller commits."""
    for entry in PROVIDER_CATALOG:
        await conn.execute(_UPSERT_SQL, _params(entry))
    return len(PROVIDER_CATALOG)
