"""Unit tests for ArkeselSMSProvider's balance-fetch refactor: verify_credentials()
must behave exactly as before (Test Connection display string, same error text),
and the new get_sms_balance() must return the raw numeric value for the
reconciliation job. No server, no DB — httpx.MockTransport, same pattern as
test_payment_providers.py.
"""
from decimal import Decimal

import httpx
import pytest

from src.modules.sms.providers.arkesel import ArkeselSMSProvider


def _provider(handler) -> ArkeselSMSProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://sms.arkesel.com")
    return ArkeselSMSProvider(api_key="test-key", sender_id="SENDER", client=client)


@pytest.mark.asyncio
async def test_verify_credentials_returns_balance_string_unchanged():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "main_balance": "12.50", "sms_balance": "417"})

    detail = await _provider(handler).verify_credentials()
    assert detail == "balance 12.50"


@pytest.mark.asyncio
async def test_verify_credentials_raises_with_arkesel_message_unchanged():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Invalid key", "status": "error"})

    with pytest.raises(ValueError, match="Invalid key"):
        await _provider(handler).verify_credentials()


@pytest.mark.asyncio
async def test_get_sms_balance_returns_raw_decimal():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "main_balance": "12.50", "sms_balance": "417"})

    balance = await _provider(handler).get_sms_balance()
    assert balance == Decimal("417")


@pytest.mark.asyncio
async def test_get_sms_balance_returns_none_when_field_absent():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "main_balance": "12.50"})

    balance = await _provider(handler).get_sms_balance()
    assert balance is None


@pytest.mark.asyncio
async def test_get_sms_balance_raises_on_rejected_credentials():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Invalid key", "status": "error"})

    with pytest.raises(ValueError, match="Invalid key"):
        await _provider(handler).get_sms_balance()


@pytest.mark.asyncio
async def test_get_sms_balance_reads_nested_data_scope():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {"sms_balance": "9"}})

    balance = await _provider(handler).get_sms_balance()
    assert balance == Decimal("9")
