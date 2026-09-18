import re

GHANA_PHONE_ERROR = "Phone must be a valid Ghana mobile number (e.g. 0244123456)"


def normalize_ghana_phone(value: str) -> str:
    """Normalize a Ghana mobile number to 233XXXXXXXXX, or raise ValueError.

    Accepts 233XXXXXXXXX or 0XXXXXXXXX (10 digits starting 02x/05x), with any
    spacing/punctuation. Shared by the public application form and every path
    that creates an operator admin, so all stored numbers have one shape.
    """
    digits = re.sub(r"\D", "", value or "")
    if re.match(r"^233[0-9]{9}$", digits):
        return digits
    if re.match(r"^0[2-9][0-9]{8}$", digits):
        return "233" + digits[1:]
    raise ValueError(GHANA_PHONE_ERROR)


def mask_phone(phone: str | None) -> str:
    """233244123456 -> +233 •••• ••• 456 — safe to show back to a user."""
    if not phone:
        return ""
    return f"+{phone[:3]} •••• ••• {phone[-3:]}"
