from cryptography.fernet import Fernet
from src.config import get_settings

settings = get_settings()
_fernet = Fernet(settings.encryption_key.encode())


def encrypt_secret(secret: str) -> str:
    """Encrypt a NAS shared secret for storage."""
    return _fernet.encrypt(secret.encode()).decode()


def decrypt_secret(encrypted: str) -> str:
    """Decrypt a stored NAS shared secret."""
    return _fernet.decrypt(encrypted.encode()).decode()
