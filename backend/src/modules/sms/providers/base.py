from __future__ import annotations

from abc import ABC, abstractmethod

from src.modules.sms.types import SMSSendResult


class SMSProvider(ABC):
    @abstractmethod
    async def send(self, to: str, message: str) -> SMSSendResult:
        ...

    async def verify_credentials(self) -> str | None:
        """Check the stored credentials authenticate against the provider.

        Return on success; raise (any exception) on failure — the message is
        surfaced to the operator as the "test connection" result. Not every SMS
        gateway exposes a no-cost verification call; a provider without one
        inherits this and the credential ``/test`` endpoint reports that testing
        is unavailable rather than sending a chargeable message.

        A provider whose check also reveals an account balance may return a
        short detail string (e.g. ``"balance GHS 0.88"``) — the ``/test``
        endpoint appends it to the success message. Returning ``None`` means
        "verified, nothing extra to show" and is the norm.
        """
        raise NotImplementedError

