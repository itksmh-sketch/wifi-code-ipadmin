import re

PASSWORD_POLICY_MESSAGE = (
    "Password must be at least 8 characters and include an uppercase letter, "
    "a lowercase letter and a number."
)


def password_policy_error(password: str) -> str | None:
    """None when the password meets the policy, else the message to show."""
    if (
        len(password or "") < 8
        or not re.search(r"[A-Z]", password)
        or not re.search(r"[a-z]", password)
        or not re.search(r"[0-9]", password)
    ):
        return PASSWORD_POLICY_MESSAGE
    if len(password) > 128:
        return "Password must be at most 128 characters."
    return None
