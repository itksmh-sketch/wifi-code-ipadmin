"""Per-model field lists for audit ``changes``.

One entry per audited model, in one place, so what a row may say about a model
is decided once rather than at every call site:

  * ``tracked``: the columns a change row looks at. A column not listed here
    never appears in ``changes`` at all, so a new column is invisible until
    someone adds it on purpose.
  * ``shown``: the tracked columns whose before/after values are written.
    Every other tracked column that changed is recorded as "changed".

A secret-looking column (nas_secret_plain, api_password_encrypted, …) may be
tracked, so a row can say it changed, but is never shown: _fields leaves it
out of ``shown`` and redaction writes "[redacted]" in place of its value.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from src.modules.audit.redact import REDACTED, diff, is_secret_name, snapshot


@dataclass(frozen=True)
class ModelFields:
    model: str
    tracked: tuple[str, ...]
    shown: frozenset[str]

    def snapshot(self, obj: Any) -> dict[str, Any] | None:
        return None if obj is None else snapshot(obj, self.tracked)

    def diff(self, before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any] | None:
        return diff(before, after, shown=self.shown)


def _fields(model: str, tracked: tuple[str, ...], hidden: tuple[str, ...] = ()) -> ModelFields:
    shown = {name for name in tracked if name not in hidden and not is_secret_name(name)}
    return ModelFields(model, tracked, frozenset(shown))


PLAN = _fields("Plan", (
    "name", "type", "site_id", "duration_minutes", "data_limit_mb",
    "download_speed_kbps", "upload_speed_kbps", "price_ghs", "is_active",
))

# Never code, username or password: a voucher is bearer credit.
VOUCHER = _fields("Voucher", ("status", "printed_at"))

TOWN = _fields("Town", ("name", "region"))

SITE = _fields("Site", ("name", "address", "town_id"))

# nas_secret_plain, not nas_secret: the encrypted column differs on every
# write (Fernet), the plain one only when the secret really changed. Either
# way the row says "[redacted]", never the value.
ROUTER = _fields("Router", ("name", "site_id", "ip_address", "nas_identifier", "nas_secret_plain", "is_active"))

ROUTER_REMOVAL = _fields("Router", ("is_active", "removed_at"))

# The peer keys are secret-named, so they show as "[redacted]" when they change.
ROUTER_WIREGUARD = _fields("Router", ("wg_enabled", "wg_tunnel_ip", "wg_peer_public_key", "wg_peer_private_key_encrypted"))

# The password is re-sent (and re-encrypted) on every save, so it always shows
# as "[redacted]" there: it was set again, whatever its value.
ROUTER_CREDENTIAL = _fields("RouterCredential", ("api_username", "api_port", "use_ssl", "api_password_encrypted"))

# template_data is RouterOS configuration an operator wrote; it can hold
# anything, so a row says only that it changed.
CONFIG_TEMPLATE = _fields("ConfigTemplate", ("name", "description", "is_default", "template_data"), hidden=("template_data",))

# Portal branding. The contact phone is a real number: a row says only that it
# changed.
BRANDING = _fields("ISPOperator", (
    "portal_display_name", "primary_color", "accent_color", "background_gradient_start",
    "portal_welcome_message", "portal_template", "portal_contact_phone", "portal_contact_email", "logo_url",
), hidden=("portal_contact_phone",))

VOUCHER_SMS_TEMPLATE = _fields("ISPOperator", ("voucher_sms_template",))

# password_hash is secret-named: "[redacted]" when a new password was set,
# absent otherwise. The phone shows only as changed.
RESELLER = _fields("Reseller", ("name", "email", "phone", "role", "town_id", "site_id", "is_active", "password_hash"),
                   hidden=("phone",))

COMMISSION_RULE = _fields("CommissionRule", ("reseller_id", "plan_id", "type", "value", "is_active"))

# Platform side. An operator's contact phone is a real number: a row says
# only that it changed.
OPERATOR_PROFILE = _fields("ISPOperator", ("name", "contact_email", "contact_phone"), hidden=("contact_phone",))

OPERATOR_CREATE = _fields("ISPOperator", (
    "name", "slug", "contact_email", "contact_phone", "status", "billing_status", "monthly_fee_ghs", "trial_ends_at",
), hidden=("contact_phone",))

OPERATOR_STATUS = _fields("ISPOperator", (
    "status", "billing_status", "suspension_reason", "cancelled_at", "hotspot_service_ends_at",
    "hotspot_service_cut_off_at",
))

OPERATOR_BILLING = _fields("ISPOperator", ("monthly_fee_ghs", "trial_ends_at"))

PROVIDER_CATALOG = _fields("ProviderCatalogEntry", ("is_available", "platform_rate_per_segment"))

# The email body is HTML of any length: a row says only that it changed. The
# subject and the plain text (the SMS) show their values.
NOTIFICATION_TEMPLATE = _fields("PlatformNotificationTemplate", ("subject", "body_text", "body_html"),
                                hidden=("body_html",))

PLATFORM_OWNER_PROFILE = _fields("PlatformOwner", ("name",))

ALL = (PLAN, VOUCHER, TOWN, SITE, ROUTER, ROUTER_REMOVAL, ROUTER_WIREGUARD, ROUTER_CREDENTIAL, CONFIG_TEMPLATE,
       BRANDING, VOUCHER_SMS_TEMPLATE, RESELLER, COMMISSION_RULE, OPERATOR_PROFILE, OPERATOR_CREATE,
       OPERATOR_STATUS, OPERATOR_BILLING, PROVIDER_CATALOG, NOTIFICATION_TEMPLATE, PLATFORM_OWNER_PROFILE)


# Payment and SMS provider credentials are one encrypted blob of catalog
# fields, so they are compared field by field after decrypting. Only these
# plain identifiers show their values; any other field that changed is
# "changed", and "[redacted]" when the catalog marks it secret or its name
# looks secret (public_key included: a row never carries a key).
CREDENTIAL_SHOWN = frozenset({"username", "from", "sender_id"})


def credential_diff(before: dict | None, after: dict | None, *, secret_fields: Iterable[str] = ()) -> dict | None:
    """Field changes between two decrypted credential blobs, or None. A field
    whose value is the same on both sides is not listed, secret or not."""
    changes = diff(before, after, shown=CREDENTIAL_SHOWN)
    if not changes:
        return None
    secret = set(secret_fields)
    return {name: (REDACTED if name in secret else value) for name, value in changes.items()}
