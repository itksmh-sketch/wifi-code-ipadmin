import asyncio
import hashlib
import hmac
import json
import time
from decimal import Decimal

import httpx
import pytest

from src.config import Settings
from src.modules.payments.providers.airteltigo import AirtelTigoMockProvider
from src.modules.payments.providers.mtn import MTNMoMoProvider
from src.modules.payments.providers.paystack import PaystackProvider
from src.modules.payments.providers.vodafone import VodafoneCashMockProvider
from src.modules.payments.types import PaymentNextAction, PaymentStatus


def _settings() -> Settings:
    return Settings(
        mtn_momo_base_url="https://sandbox.momo.test",
        mtn_momo_collection_subscription_key="sub-key",
        mtn_momo_api_user="api-user",
        mtn_momo_api_key="api-key",
        mtn_momo_environment="sandbox",
        paystack_secret_key="paystack-secret",
        paystack_callback_url="https://example.com/callback",
        vodafone_cash_merchant_id="merchant-1",
    )


@pytest.mark.asyncio
async def test_mtn_token_cached_until_expiry():
    calls = {"token": 0, "requesttopay": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/collection/token/"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        if request.url.path.endswith("/collection/v1_0/requesttopay"):
            calls["requesttopay"] += 1
            return httpx.Response(202, json={})
        raise AssertionError(f"Unexpected path {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://sandbox.momo.test")
    provider = MTNMoMoProvider(_settings(), client=client)

    await provider.initiate(Decimal("3.00"), "233244123456", "p1", "s1", "ref-1", "mtn_momo")
    await provider.initiate(Decimal("3.00"), "233244123456", "p1", "s1", "ref-2", "mtn_momo")
    assert calls["token"] == 1
    assert calls["requesttopay"] == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_vodafone_mock_realistic_shape_and_delay(monkeypatch):
    sleep_args = []

    async def fake_sleep(seconds: float):
        sleep_args.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    provider = VodafoneCashMockProvider(_settings())
    initiation = await provider.initiate(Decimal("7.50"), "233244123456", "plan-1", "site-1", "ref-1", "vodafone_cash")
    verification = await provider.verify("VOD-ref-1")
    assert 2 in sleep_args
    assert initiation.provider_reference.startswith("VOD-")
    assert initiation.status == PaymentStatus.PENDING
    assert verification.provider_reference == "VOD-ref-1"


@pytest.mark.asyncio
async def test_airteltigo_mock_realistic_shape_and_delay(monkeypatch):
    sleep_args = []

    async def fake_sleep(seconds: float):
        sleep_args.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    provider = AirtelTigoMockProvider(_settings())
    initiation = await provider.initiate(Decimal("7.50"), "233244123456", "plan-1", "site-1", "ref-1", "airteltigo")
    verification = await provider.verify("AT-ref-1")
    assert 2 in sleep_args
    assert initiation.provider_reference.startswith("AT-")
    assert initiation.status == PaymentStatus.PENDING
    assert verification.provider_reference == "AT-ref-1"


@pytest.mark.asyncio
async def test_paystack_webhook_uses_raw_body_hmac():
    settings = _settings()
    provider = PaystackProvider(settings=settings)
    raw_body = json.dumps({"event": "charge.success", "data": {"reference": "ref-raw"}}).encode("utf-8")
    signature = hmac.new(settings.paystack_secret_key.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    result = await provider.handle_webhook({"x-paystack-signature": signature}, raw_body)
    assert result.internal_reference == "ref-raw"
    assert result.status == PaymentStatus.SUCCESS


@pytest.mark.asyncio
async def test_paystack_webhook_invalid_signature_rejected():
    provider = PaystackProvider(settings=_settings())
    raw_body = b'{"event":"charge.success","data":{"reference":"ref-bad"}}'
    with pytest.raises(ValueError):
        await provider.handle_webhook({"x-paystack-signature": "bad"}, raw_body)


@pytest.mark.asyncio
async def test_paystack_card_uses_transaction_initialize():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "status": True,
                "message": "Authorization URL created",
                "data": {
                    "authorization_url": "https://checkout.paystack.com/test",
                    "reference": "ref-card",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.paystack.co")
    provider = PaystackProvider(settings=_settings(), client=client)

    result = await provider.initiate(Decimal("2.00"), None, "plan-1", "site-1", "ref-card", "card")

    assert seen["path"] == "/transaction/initialize"
    assert seen["body"]["email"] == "pay_ref-card@hotspot.yourisp.com"
    assert seen["body"]["amount"] == 200
    assert result.next_action == PaymentNextAction.OPEN_URL
    assert result.authorization_url == "https://checkout.paystack.com/test"
    assert result.payment_channel == "card"
    await client.aclose()


@pytest.mark.asyncio
async def test_paystack_mobile_money_uses_charge_payload_and_waits_for_approval():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "status": True,
                "message": "Charge attempted",
                "data": {
                    "status": "send_otp",
                    "reference": "ref-momo",
                    "channel": "mobile_money",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.paystack.co")
    provider = PaystackProvider(settings=_settings(), client=client)

    result = await provider.initiate(Decimal("7.50"), "+233 24 412 3456", "plan-1", "site-1", "ref-momo", "vodafone_cash")

    assert seen["path"] == "/charge"
    assert seen["body"] == {
        "email": "233244123456@hotspot.yourisp.com",
        "amount": 750,
        "currency": "GHS",
        "reference": "ref-momo",
        "mobile_money": {
            "phone": "233244123456",
            "provider": "vodafone",
        },
    }
    assert result.status == PaymentStatus.PENDING
    assert result.next_action == PaymentNextAction.WAIT
    await client.aclose()


@pytest.mark.asyncio
async def test_paystack_http_error_raises_value_error_message():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Invalid mobile money provider"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.paystack.co")
    provider = PaystackProvider(settings=_settings(), client=client)

    with pytest.raises(ValueError, match="Invalid mobile money provider"):
        await provider.initiate(Decimal("7.50"), "233244123456", "plan-1", "site-1", "ref-bad", "mtn_momo")
    await client.aclose()


@pytest.mark.asyncio
async def test_paystack_rejects_amounts_below_one_ghs():
    provider = PaystackProvider(settings=_settings())

    with pytest.raises(ValueError, match="Amount too small for Paystack processing"):
        await provider.initiate(Decimal("0.99"), "233244123456", "plan-1", "site-1", "ref-small", "mtn_momo")
