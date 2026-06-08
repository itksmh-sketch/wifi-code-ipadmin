from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(slots=True)
class EmailSendResult:
    success: bool
    provider_reference: str | None = None
    error: str | None = None


class EmailProvider(ABC):
    @abstractmethod
    async def send(self, to: str, subject: str, body_html: str, body_text: str) -> EmailSendResult:
        ...
