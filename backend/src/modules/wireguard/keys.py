"""WireGuard key generation.

Keys are generated with the Python ``cryptography`` library's X25519 primitives
so there is no dependency on the ``wg`` binary inside the backend container.
A WireGuard key is simply a 32-byte Curve25519 key, base64-encoded.
"""
import base64

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def generate_wireguard_keypair() -> tuple[str, str]:
    """Generate a WireGuard keypair.

    Returns ``(private_key_b64, public_key_b64)`` — both standard base64,
    identical in format to the output of ``wg genkey`` / ``wg pubkey``.
    """
    private_key = X25519PrivateKey.generate()
    private_bytes = private_key.private_bytes_raw()
    public_bytes = private_key.public_key().public_bytes_raw()

    private_b64 = base64.b64encode(private_bytes).decode()
    public_b64 = base64.b64encode(public_bytes).decode()

    return private_b64, public_b64


def is_valid_wireguard_key(key: str) -> bool:
    """True if ``key`` is a well-formed base64-encoded 32-byte WireGuard key."""
    try:
        raw = base64.b64decode(key, validate=True)
    except (ValueError, TypeError):
        return False
    return len(raw) == 32
