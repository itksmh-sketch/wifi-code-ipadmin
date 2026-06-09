from cryptography.fernet import Fernet, InvalidToken
from src.config import get_settings

settings = get_settings()

try:
    _fernet = Fernet(settings.encryption_key.encode())
except Exception:
    raise RuntimeError(
        "ENCRYPTION_KEY is invalid. Generate a valid key with: "
        "python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )


def encrypt_secret(secret: str) -> str:
    return _fernet.encrypt(secret.encode()).decode()


def decrypt_secret(encrypted: str) -> str:
    return _fernet.decrypt(encrypted.encode()).decode()
