import logging
from typing import Any

logger = logging.getLogger("payments.providers")


def redact_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(token in lowered for token in ("key", "secret", "token", "authorization")):
        return "***redacted***"
    return value


def redact_dict(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: redact_value(k, v) for k, v in payload.items()}
