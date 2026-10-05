from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import httpx

from src.modules.sms.types import SMSSendResult


def key_fingerprint(secret: str | None) -> str:
    """sha256[:12] of a credential, for logs — identifies which key sent a
    message without ever printing it. Matches the fingerprints used when
    comparing stored keys against a provider account."""
    if not secret:
        return "none"
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def mask_msisdn(raw: str | None) -> str:
    """``23320****514``: enough to tell numbers and networks apart in a log,
    not enough to text the customer."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) < 9:
        return "***"
    return f"{digits[:5]}****{digits[-3:]}"


@asynccontextmanager
async def http_client(
    injected: Optional[httpx.AsyncClient], *, base_url: str, timeout: float = 10.0
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield the injected client as-is (tests own and close it), else a client
    owned by this call and closed on exit — a provider instance lives for one
    send, so an unclosed per-call client leaks its connection pool."""
    if injected is not None:
        yield injected
        return
    async with httpx.AsyncClient(timeout=timeout, base_url=base_url) as client:
        yield client


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

