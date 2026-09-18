import re

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_email(value: str) -> str:
    """Trimmed, lowercased email, or ValueError. Admin logins compare this form."""
    email = (value or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError("Invalid email address")
    return email
