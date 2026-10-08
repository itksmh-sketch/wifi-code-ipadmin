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

ALL = (PLAN, VOUCHER, TOWN, SITE, ROUTER, ROUTER_REMOVAL, ROUTER_WIREGUARD, ROUTER_CREDENTIAL, CONFIG_TEMPLATE)


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
