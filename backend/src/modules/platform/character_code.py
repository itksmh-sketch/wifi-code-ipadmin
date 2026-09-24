"""The platform owner's character code: generation, storage form, and the
per-position check used by the login challenge.

The code is shown to the owner once and never stored. What is stored is one
keyed digest per position (see migration 053 for the column):

    {"v": 1, "salt": "<hex>", "digests": ["<hex>", ...]}
    digest[i] = HMAC-SHA256(K, "<owner_id>:<salt>:<i>:<char_i>")

K is derived from ENCRYPTION_KEY by HKDF with a fixed label, so it is a
separate key from the Fernet key used for stored secrets, even though both
come from the same setting. Recovering the code needs the database AND the
app key. With only a dump, a ~31-symbol alphabet per position is useless to
brute-force without K. Per-character bcrypt would not be: 31 bcrypt calls per
position recovers the whole code in seconds.

Consequence for operations: rotating ENCRYPTION_KEY invalidates the stored
digests, so the owner's code stops verifying and must be reset (break-glass
script or the forgot-code flow).

Positions are 0-based here and in storage; the API speaks 1-based positions
("character 3") because that is what the owner sees.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from src.config import get_settings

# Uppercase letters and digits minus the ones that read alike on screen or
# paper: 0/O, 1/I/L. Input is upper-cased, so the owner can type either case.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 12
POSITIONS_PER_CHALLENGE = 3
STORAGE_VERSION = 1

_HKDF_INFO = b"platform-owner-character-code-v1"


@lru_cache(maxsize=4)
def _derive_key(encryption_key: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(encryption_key.encode())


def _key() -> bytes:
    return _derive_key(get_settings().encryption_key)


def _digest(key: bytes, owner_id: uuid.UUID, salt: str, position: int, char: str) -> str:
    message = f"{owner_id}:{salt}:{position}:{char}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def generate_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def build_storage(owner_id: uuid.UUID, code: str) -> dict:
    """The challenge_hashes value for ``code``. A fresh salt every time, so a
    regenerated code shares no digests with the one it replaces."""
    if len(code) != CODE_LENGTH or any(c not in ALPHABET for c in code):
        raise ValueError("not a valid character code")
    salt = secrets.token_hex(16)
    key = _key()
    return {
        "v": STORAGE_VERSION,
        "salt": salt,
        "digests": [_digest(key, owner_id, salt, i, c) for i, c in enumerate(code)],
    }


def code_length(storage: dict) -> int:
    return len(storage["digests"])


def choose_positions(storage: dict) -> list[int]:
    """POSITIONS_PER_CHALLENGE distinct 0-based positions, sorted."""
    return sorted(secrets.SystemRandom().sample(range(code_length(storage)), POSITIONS_PER_CHALLENGE))


def normalize_char(value: str) -> str:
    return (value or "").strip().upper()


def check_positions(owner_id: uuid.UUID, storage: dict, answers: dict[int, str]) -> bool:
    """True only if every (0-based position -> character) answer matches.

    Every position is computed and compared even after a miss, and the
    comparison is constant-time, so neither the response time nor anything
    else tells the caller WHICH position was wrong.
    """
    if not answers or storage.get("v") != STORAGE_VERSION:
        return False
    key, salt, digests = _key(), storage["salt"], storage["digests"]
    ok = True
    for position, value in answers.items():
        char = normalize_char(value)
        if not (0 <= position < len(digests)) or len(char) != 1:
            ok = False
            char = "?"  # still spend the HMAC, keep timing flat
        candidate = _digest(key, owner_id, salt, position if 0 <= position < len(digests) else 0, char)
        expected = digests[position] if 0 <= position < len(digests) else digests[0]
        ok = hmac.compare_digest(candidate, expected) and ok
    return ok
