"""What an audit row may say about a change.

Two rules, and the second always wins:

1. ``diff`` shows before/after values only for fields the call site lists in
   ``shown``. Every other changed field is recorded as "changed". A field
   nobody thought about therefore can't leak: forgetting to list it hides it.
2. A field whose name looks like a secret (password, pin, secret, token, key,
   code, hash, ...) is never shown, even when listed, and its value is
   replaced in ``detail`` too. This is matched on name parts, so
   ``nas_secret_plain``, ``api_key`` and ``pinHash`` are all caught.

Over-matching is deliberate: a voucher code or a WireGuard public key shows as
"[redacted]", which costs a little readability and nothing else.

One exception, by exact key and exact shape: ``code_suffix`` passes when its
value is what ``mask_code`` makes ("…NV9W"), so a single-voucher row can say
which voucher it was. Anything else under that key is still redacted.
"""
from __future__ import annotations

import datetime
import decimal
import enum
import re
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

CHANGED = "changed"
REDACTED = "[redacted]"
STRING_MAX = 500
_MAX_DEPTH = 5

SECRET_NAME_PARTS = frozenset({
    "password", "passwd", "pwd", "pin", "secret", "secrets", "token", "tokens", "key", "keys",
    "apikey", "code", "codes", "otp", "hash", "hashes", "answer", "challenge", "digest", "digests",
    "salt", "jti", "credential", "credentials", "cookie", "authorization", "signature", "private",
    "encrypted", "ciphertext",
})

CODE_SUFFIX_KEY = "code_suffix"
CODE_SUFFIX_LEN = 4
_MASKED_RE = re.compile(r"^…[A-Za-z0-9]{0,%d}$" % CODE_SUFFIX_LEN)

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def is_secret_name(name: str) -> bool:
    parts = _SPLIT_RE.split(_CAMEL_RE.sub("_", str(name)).lower())
    return any(part in SECRET_NAME_PARTS for part in parts)


def mask_code(code: str | None) -> str:
    """The last few characters of a voucher code, for telling vouchers apart
    in a row: "…NV9W". Never more."""
    chars = re.sub(r"[^A-Za-z0-9]", "", code or "")
    return "…" + chars[-CODE_SUFFIX_LEN:]


def _redacted_value(key: Any, value: Any) -> bool:
    if key == CODE_SUFFIX_KEY:
        return not (isinstance(value, str) and _MASKED_RE.match(value))
    return is_secret_name(key)


def jsonable(value: Any) -> Any:
    """A JSON-safe, size-capped copy of a scalar."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, enum.Enum):
        return jsonable(value.value)
    if isinstance(value, (uuid.UUID, decimal.Decimal)):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    text = str(value)
    return text if len(text) <= STRING_MAX else text[:STRING_MAX] + "…"


def redact_detail(value: Any, _depth: int = 0) -> Any:
    """Copy ``value`` with every secret-named key's value replaced."""
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(k): (REDACTED if _redacted_value(k, v) else redact_detail(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_detail(v, _depth + 1) for v in value]
    return jsonable(value)


def snapshot(obj: Any, fields: Iterable[str]) -> dict[str, Any]:
    """Read ``fields`` off a model instance, for a later ``diff``."""
    return {name: getattr(obj, name, None) for name in fields}


def diff(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *,
    shown: Iterable[str] = (),
) -> dict[str, Any] | None:
    """The fields that differ between two snapshots, or None if none do.

    A key missing on one side counts as None there, so a create is
    ``diff(None, after)`` and a delete ``diff(before, None)``.
    """
    before = before or {}
    after = after or {}
    visible = {name for name in shown if not is_secret_name(name)}
    changes: dict[str, Any] = {}
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if name in visible:
            changes[name] = {"before": jsonable(old), "after": jsonable(new)}
        else:
            changes[name] = CHANGED
    return changes or None


# A run this long copied out of a secret is redacted even when the rest of the
# secret isn't there: a provider may quote only the start of a key it rejects.
FRAGMENT_MIN = 8
_SCRUB_SCAN_MAX = 20_000
# Payment-provider key shapes, redacted whoever's key they are.
_KEY_SHAPED_RE = re.compile(
    r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]+|\bFLW(?:SECK|PUBK)(?:_TEST)?-[A-Za-z0-9-]+"
)


def scrub(text: Any, secrets: Iterable[str | None] = (), *, cap: bool = True) -> str | None:
    """An error message fit for a row: every secret value used in the call is
    replaced, then the text is capped.

    Name-based redaction can't see inside free text, and a router or provider
    may echo back what it was sent (a RADIUS secret in a rejected command, a
    Paystack key in "Invalid key"). Three passes, all before the cap, so a
    secret cut at the limit can't leave a prefix behind:

    1. each whole secret, longest first, so one secret inside another goes whole;
    2. any run of FRAGMENT_MIN or more characters copied out of a secret;
    3. anything shaped like a payment-provider key.

    ``cap=False`` keeps the whole text, for a value that must stay intact
    (a stored RouterOS command); only an error message is capped.
    """
    if text is None:
        return None
    text = str(text)
    if cap:
        text = text[:_SCRUB_SCAN_MAX]
    values = sorted({str(s) for s in secrets if s}, key=len, reverse=True)
    for secret in values:
        text = text.replace(secret, REDACTED)
    text = _scrub_fragments(text, [s for s in values if len(s) >= FRAGMENT_MIN])
    text = _KEY_SHAPED_RE.sub(REDACTED, text)
    return jsonable(text) if cap else text


def scrub_values(value: Any, secrets: Iterable[str | None], _depth: int = 0) -> Any:
    """``scrub`` every string inside a JSON-like value, uncapped, keeping its
    shape: for stored command lists, whose params may carry a secret under any
    key (a config template can put the NAS secret anywhere)."""
    secrets = list(secrets)
    if _depth > 20:
        return value
    if isinstance(value, str):
        return scrub(value, secrets, cap=False)
    if isinstance(value, Mapping):
        return {k: scrub_values(v, secrets, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_values(v, secrets, _depth + 1) for v in value]
    return value


def _scrub_fragments(text: str, secrets: list[str]) -> str:
    if not secrets:
        return text
    grams = {s[i:i + FRAGMENT_MIN] for s in secrets for i in range(len(s) - FRAGMENT_MIN + 1)}
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i:i + FRAGMENT_MIN] in grams:
            end = i + FRAGMENT_MIN
            while end < len(text) and any(text[i:end + 1] in s for s in secrets):
                end += 1
            out.append(REDACTED)
            i = end
        else:
            out.append(text[i])
            i += 1
    return "".join(out)
