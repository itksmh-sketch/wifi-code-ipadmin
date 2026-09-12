from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import httpx

from src.modules.sms.providers.base import SMSProvider
from src.modules.sms.types import SMSSendResult

logger = logging.getLogger("sms.providers.arkesel")


def _clip(text: str, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _gh_msisdn(raw: str) -> str:
    """Arkesel v2 wants a bare ``233XXXXXXXXX`` (no ``+``). Upstream
    (``payments.service.normalize_phone``) already hands us that; also fold a
    local ``0XXXXXXXXX`` just in case."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        digits = "233" + digits[1:]
    return digits


def _extract_message_id(data: Any) -> str | None:
    """Pull a per-message id out of Arkesel's ``data`` block. Confirmed live
    (2026-09-10) as a list ``[{"id": ..., "recipient": ...}]``; also tolerate a
    flat ``{"id": ...}`` or a keyed map ``{"0": {"id": ...}}`` in case a revision
    changes it."""
    if isinstance(data, dict):
        if data.get("id"):
            return str(data["id"])
        for v in data.values():
            if isinstance(v, dict) and v.get("id"):
                return str(v["id"])
    if isinstance(data, list):
        for v in data:
            if isinstance(v, dict) and v.get("id"):
                return str(v["id"])
    return None


def _balance_detail(payload: Any) -> str | None:
    """Best-effort "balance …" string from /clients/balance-details. Never
    raises — a parse miss just means the success message shows no balance."""
    if not isinstance(payload, dict):
        return None
    scopes = [payload, payload.get("data") if isinstance(payload.get("data"), dict) else {}]
    for scope in scopes:
        for key in ("main_balance", "sms_balance", "balance"):
            val = scope.get(key)
            if val not in (None, ""):
                return f"balance {_clip(str(val), 40)}"
    return None


class ArkeselSMSProvider(SMSProvider):
    """
    Arkesel SMS API v2.

    POST https://sms.arkesel.com/api/v2/sms/send
    Header: ``api-key: <api_key>``
    JSON body: ``{ "sender": <id>, "message": <text>, "recipients": [<msisdn>] }``

    Acceptance is signalled explicitly, unlike Africa's Talking (shapes confirmed
    against the live API, 2026-09-10):
      * accepted -> HTTP 200 ``{"status": "success",
        "data": [{"id": <uuid>, "recipient": "233..."}],
        "main_balance": <num>, "sms_balance": <num>}``
      * rejected -> HTTP 4xx (e.g. 401 ``{"message": "Invalid key",
        "status": "error"}``) and/or a 200 body with ``status": "error"``
        (unregistered sender ID, invalid number, insufficient balance, ...).
        The ``message`` is surfaced verbatim.

    The send response echoes ``main_balance`` / ``sms_balance`` but carries no
    per-message credit/segment count — metered billing (future) must read those
    balance fields or the delivery callback, not this response.

    The synchronous response only confirms the message was *queued*. Final
    delivery status (DELIVERED / NOT_DELIVERED / ...) arrives asynchronously at a
    ``callback_url`` — not wired here, same as Hubtel / Africa's Talking.

    ``verify_credentials()`` -> GET /api/v2/clients/balance-details (no cost, no
    SMS); returns a "balance …" detail string for the Test-connection result.
    ``get_sms_balance()`` hits the same endpoint for the raw numeric
    ``sms_balance`` instead, for the platform-wide reconciliation job.
    """

    _BASE_URL = "https://sms.arkesel.com"
    # Arkesel v2 uses `status: "success"`; older revisions used `code: "ok"`.
    _OK_STATUSES = {"success", "ok"}

    def __init__(
        self,
        *,
        api_key: str,
        sender_id: str,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.api_key = api_key
        self.sender_id = sender_id
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=10.0, base_url=self._BASE_URL)

    async def _get_balance_payload(self) -> dict:
        """Raw parsed /clients/balance-details response. Shared by
        verify_credentials() (Test Connection, wants a display string) and
        get_sms_balance() (reconciliation, wants the raw number) so there is
        one fetch+error-handling path, not two."""
        if not self.api_key:
            raise ValueError("Arkesel API key is required")
        client = await self._get_client()
        try:
            resp = await client.get(
                "/api/v2/clients/balance-details",
                headers={"api-key": self.api_key, "Accept": "application/json"},
            )
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Could not reach Arkesel: {_clip(str(e))}") from e
        try:
            payload = resp.json()
        except Exception:
            payload = None

        if resp.status_code == 200 and not (
            isinstance(payload, dict) and str(payload.get("status", "success")).lower() == "error"
        ):
            return payload if isinstance(payload, dict) else {}

        reason = None
        if isinstance(payload, dict):
            reason = payload.get("message") or payload.get("errorMessage") or payload.get("error")
        reason = reason or resp.text or f"HTTP {resp.status_code}"
        raise ValueError(f"Arkesel rejected the credentials: {_clip(reason)}")

    async def verify_credentials(self) -> str | None:
        payload = await self._get_balance_payload()
        return _balance_detail(payload)

    async def get_sms_balance(self) -> Decimal | None:
        """Raw sms_balance (a segment/credit count) from Arkesel's own
        account — for the aggregate reconciliation check only. Never use this
        for per-message billing (see class docstring): it's platform-wide,
        not per-operator, and carries no attribution."""
        payload = await self._get_balance_payload()
        scopes = [payload, payload.get("data") if isinstance(payload.get("data"), dict) else {}]
        for scope in scopes:
            val = scope.get("sms_balance")
            if val not in (None, ""):
                try:
                    return Decimal(str(val))
                except InvalidOperation:
                    return None
        return None

    async def send(self, to: str, message: str) -> SMSSendResult:
        if not (self.api_key and self.sender_id):
            return SMSSendResult(success=False, error="arkesel_not_configured")

        body = {"sender": self.sender_id, "message": message, "recipients": [_gh_msisdn(to)]}
        headers = {"api-key": self.api_key, "Accept": "application/json"}
        client = await self._get_client()
        try:
            resp = await client.post("/api/v2/sms/send", headers=headers, json=body)
            try:
                payload = resp.json()
            except Exception:
                payload = None

            if resp.status_code >= 400:
                reason = None
                if isinstance(payload, dict):
                    reason = payload.get("message") or payload.get("error") or payload.get("errorMessage")
                reason = reason or resp.text
                logger.warning("arkesel_sms_send_failed status=%s body=%s", resp.status_code, _clip(reason, 500))
                err = f"arkesel_http_{resp.status_code}"
                if reason:
                    err += f": {_clip(reason)}"
                return SMSSendResult(success=False, error=err)

            # HTTP 2xx: Arkesel still reports accept/reject in the body. Do NOT
            # treat a bare 2xx as success (the Africa's Talking lesson).
            status = str(payload.get("status") or payload.get("code") or "").lower() if isinstance(payload, dict) else ""
            if status not in self._OK_STATUSES:
                reason = None
                if isinstance(payload, dict):
                    reason = payload.get("message") or payload.get("error")
                reason = reason or _clip(resp.text, 200) or "message not accepted"
                logger.warning("arkesel_sms_rejected status=%s reason=%s", status or "?", _clip(reason))
                return SMSSendResult(success=False, error=f"arkesel_status_{status or 'unknown'}: {_clip(reason)}")

            provider_ref = _extract_message_id(payload.get("data"))
            logger.info("arkesel_sms_send_ok to=%s provider_reference=%s", to, provider_ref)
            return SMSSendResult(success=True, provider_reference=provider_ref)
        except Exception as e:
            logger.exception("arkesel_sms_send_exception to=%s", to)
            return SMSSendResult(success=False, error=f"arkesel_exception: {_clip(str(e))}")
