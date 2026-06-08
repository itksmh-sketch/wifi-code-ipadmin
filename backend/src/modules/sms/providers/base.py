from __future__ import annotations

from abc import ABC, abstractmethod

from src.modules.sms.types import SMSSendResult


class SMSProvider(ABC):
    @abstractmethod
    async def send(self, to: str, message: str) -> SMSSendResult:
        ...

