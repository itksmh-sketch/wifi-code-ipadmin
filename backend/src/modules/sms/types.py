from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SMSSendResult:
    success: bool
    provider_reference: str | None = None
    error: str | None = None

