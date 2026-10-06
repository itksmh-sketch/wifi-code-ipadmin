import hashlib

from cryptography.fernet import Fernet
from src.config import get_settings

settings = get_settings()

# SHA-256 of keys that have been published (the old docker-compose fallback and
# .env.example value). Anyone holding the repository can decrypt with them, so
# they are refused like a missing key. Stored as hashes so the keys themselves
# never appear in the code again.
PUBLIC_KEY_SHA256 = frozenset({
    "8600d4116676677a4ce01c01f1842a52dcff23c37623d7d30914cae4d98fe451",
})

GENERATE_HINT = (
    "Generate a key with: "
    "python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
)


def load_fernet(key: str | None) -> Fernet:
    """The Fernet for ENCRYPTION_KEY, or RuntimeError with a clear message.
    Called at import, so the backend and worker refuse to start rather than run
    without a key or with a public one."""
    if not key or not key.strip():
        raise RuntimeError(f"ENCRYPTION_KEY is not set. Refusing to start. {GENERATE_HINT}")
    if hashlib.sha256(key.encode()).hexdigest() in PUBLIC_KEY_SHA256:
        raise RuntimeError(
            "ENCRYPTION_KEY is a public example key published in this repository. "
            f"Refusing to start. {GENERATE_HINT}"
        )
    try:
        return Fernet(key.encode())
    except Exception:
        raise RuntimeError(f"ENCRYPTION_KEY is invalid. {GENERATE_HINT}")


_fernet = load_fernet(settings.encryption_key)


def encrypt_secret(secret: str) -> str:
    return _fernet.encrypt(secret.encode()).decode()


def decrypt_secret(encrypted: str) -> str:
    return _fernet.decrypt(encrypted.encode()).decode()
