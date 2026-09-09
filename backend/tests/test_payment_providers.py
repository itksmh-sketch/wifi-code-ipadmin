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
from src.modules.payments.providers.flutterwave import FlutterwaveProvider
from src.modules.payments.providers.mtn import MTNMoMoProvider
from src.modules.payments.providers.paystack import PaystackProvider
from src.modules.payments.providers.registry import build_payment_provider
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


@pytest.mark.asyncio
async def test_paystack_decline_surfaces_nested_reason_not_generic_message():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "status": False,
                "message": "Charge attempted",
                "data": {
                    "status": "failed",
                    "reference": "ref-x",
                    "message": "Declined. Please use the test mobile money number since you are doing a test transaction.",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.paystack.co")
    provider = PaystackProvider(settings=_settings(), client=client)

    with pytest.raises(ValueError, match="test mobile money number"):
        await provider.initiate(Decimal("2.00"), "233555000111", "plan-1", "site-1", "ref-x", "mtn_momo")
    await client.aclose()


@pytest.mark.asyncio
async def test_paystack_pending_uses_helpful_fallback_not_top_level_message():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": True,
                "message": "Charge attempted",
                "data": {"status": "pay_offline", "reference": "ref-p", "channel": "mobile_money"},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.paystack.co")
    provider = PaystackProvider(settings=_settings(), client=client)

    result = await provider.initiate(Decimal("2.00"), "233555000111", "plan-1", "site-1", "ref-p", "mtn_momo")
    assert result.next_action == PaymentNextAction.WAIT
    assert result.display_message == "Check your phone and approve the mobile money payment prompt."
    await client.aclose()


# ---------------------------------------------------------------------------
# Flutterwave (v3) — recorded-fixture tests, no network
# ---------------------------------------------------------------------------

def _flw(handler) -> FlutterwaveProvider:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.flutterwave.com"
    )
    return FlutterwaveProvider(
        client=client,
        secret_key="FLWSECK-test",
        public_key="FLWPUBK-test",
        webhook_secret="verifhash-test",
        callback_url="https://example.com/cb",
    )


@pytest.mark.asyncio
async def test_flutterwave_momo_charge_pending_waits():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path + ("?" + request.url.query.decode() if request.url.query else "")
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"status": "success", "data": {"status": "pending", "flw_ref": "FLW-1", "id": 99}})

    provider = _flw(handler)
    result = await provider.initiate(
        Decimal("5.00"), "233244123456", "plan-1", "site-1", "ref-momo", "mtn_momo", client_ip="41.1.2.3"
    )
    assert seen["path"] == "/v3/charges?type=mobile_money_ghana"
    assert seen["body"]["amount"] == "5.00"
    assert seen["body"]["currency"] == "GHS"
    assert seen["body"]["network"] == "MTN"
    assert seen["body"]["phone_number"] == "0244123456"
    assert seen["body"]["client_ip"] == "41.1.2.3"
    assert result.status == PaymentStatus.PENDING
    assert result.next_action == PaymentNextAction.WAIT
    assert result.provider_reference == "ref-momo"
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_flutterwave_momo_charge_redirect_opens_url():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "success",
            "data": {"status": "pending", "flw_ref": "FLW-2"},
            "meta": {"authorization": {"mode": "redirect", "redirect": "https://flutterwave.com/pay/xyz"}},
        })

    provider = _flw(handler)
    result = await provider.initiate(Decimal("5.00"), "233244123456", "p", "s", "ref-r", "vodafone_cash")
    assert result.next_action == PaymentNextAction.OPEN_URL
    assert result.authorization_url == "https://flutterwave.com/pay/xyz"
    assert result.provider_payload["authorization_url"] == "https://flutterwave.com/pay/xyz"
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_flutterwave_momo_charge_failed():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {"status": "failed", "processor_response": "insufficient funds"}})

    provider = _flw(handler)
    result = await provider.initiate(Decimal("5.00"), "233244123456", "p", "s", "ref-f", "mtn_momo")
    assert result.status == PaymentStatus.FAILED
    assert result.failure_reason == "insufficient funds"
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_flutterwave_card_uses_standard_checkout():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"status": "success", "data": {"link": "https://checkout.flutterwave.com/v3/hosted/pay/abc"}})

    provider = _flw(handler)
    result = await provider.initiate(Decimal("2.00"), None, "plan-1", "site-1", "ref-card", "card")
    assert seen["path"] == "/v3/payments"
    assert seen["body"]["payment_options"] == "card"
    assert seen["body"]["amount"] == "2.00"
    assert result.next_action == PaymentNextAction.OPEN_URL
    assert result.authorization_url == "https://checkout.flutterwave.com/v3/hosted/pay/abc"
    assert result.provider_payload["authorization_url"] == "https://checkout.flutterwave.com/v3/hosted/pay/abc"
    assert result.payment_channel == "card"
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_flutterwave_verify_successful_ghs():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/transactions/verify_by_reference"
        assert request.url.params["tx_ref"] == "ref-ok"
        return httpx.Response(200, json={"status": "success", "data": {"status": "successful", "currency": "GHS", "amount": 5, "payment_type": "mobilemoneygh"}})

    provider = _flw(handler)
    result = await provider.verify("ref-ok", expected_amount_ghs=Decimal("5.00"))
    assert result.status == PaymentStatus.SUCCESS
    assert result.amount_ghs == Decimal("5.00")
    assert result.payment_channel == "mobilemoneygh"
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_flutterwave_verify_wrong_currency_fails():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {"status": "successful", "currency": "NGN", "amount": 5000}})

    provider = _flw(handler)
    result = await provider.verify("ref-x")
    assert result.status == PaymentStatus.FAILED
    assert "currency" in (result.failure_reason or "")
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_flutterwave_verify_underpaid_fails():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {"status": "successful", "currency": "GHS", "amount": 3.5}})

    provider = _flw(handler)
    result = await provider.verify("ref-u", expected_amount_ghs=Decimal("5.00"))
    assert result.status == PaymentStatus.FAILED
    assert "underpaid" in (result.failure_reason or "")
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_flutterwave_verify_failed_and_pending():
    async def failed_handler(request):
        return httpx.Response(200, json={"status": "success", "data": {"status": "failed", "processor_response": "declined"}})

    async def pending_handler(request):
        return httpx.Response(200, json={"status": "success", "data": {"status": "pending"}})

    p1 = _flw(failed_handler)
    assert (await p1.verify("r")).status == PaymentStatus.FAILED
    await p1._client.aclose()

    p2 = _flw(pending_handler)
    assert (await p2.verify("r")).status == PaymentStatus.PENDING
    await p2._client.aclose()


@pytest.mark.asyncio
async def test_flutterwave_verify_unknown_reference_is_pending_not_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"status": "error", "message": "No transaction was found for this reference"})

    provider = _flw(handler)
    result = await provider.verify("ref-unseen")
    assert result.status == PaymentStatus.PENDING
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_flutterwave_webhook_valid_hash_returns_pending_never_trusts_status():
    provider = _flw(lambda r: httpx.Response(200))
    raw = json.dumps({"event": "charge.completed", "data": {"status": "successful", "tx_ref": "ref-wh", "amount": 5, "currency": "GHS"}}).encode("utf-8")
    result = await provider.handle_webhook({"verif-hash": "verifhash-test"}, raw)
    assert result.internal_reference == "ref-wh"
    assert result.status == PaymentStatus.PENDING  # never SUCCESS straight from the webhook
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_flutterwave_webhook_bad_hash_rejected():
    provider = _flw(lambda r: httpx.Response(200))
    raw = json.dumps({"data": {"tx_ref": "x"}}).encode("utf-8")
    with pytest.raises(ValueError):
        await provider.handle_webhook({"verif-hash": "wrong"}, raw)
    with pytest.raises(ValueError):
        await provider.handle_webhook({}, raw)
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_flutterwave_webhook_missing_tx_ref_rejected():
    provider = _flw(lambda r: httpx.Response(200))
    raw = json.dumps({"event": "charge.completed", "data": {"status": "successful"}}).encode("utf-8")
    with pytest.raises(ValueError, match="tx_ref"):
        await provider.handle_webhook({"verif-hash": "verifhash-test"}, raw)
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_flutterwave_rejects_amounts_below_one_ghs():
    provider = _flw(lambda r: httpx.Response(200))
    with pytest.raises(ValueError, match="Amount too small for Flutterwave"):
        await provider.initiate(Decimal("0.99"), "233244123456", "p", "s", "ref-s", "mtn_momo")
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_flutterwave_http_error_raises_value_error_message():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Invalid network"})

    provider = _flw(handler)
    with pytest.raises(ValueError, match="Invalid network"):
        await provider.initiate(Decimal("5.00"), "233244123456", "p", "s", "ref-b", "mtn_momo")
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_flutterwave_otp_resolves_flw_ref_then_validates():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v3/transactions/verify_by_reference":
            return httpx.Response(200, json={"status": "success", "data": {"flw_ref": "FLW-REAL-1"}})
        if request.url.path == "/v3/validate-charge":
            body = json.loads(request.content.decode("utf-8"))
            assert body == {"type": "mobile_money_ghana", "flw_ref": "FLW-REAL-1", "otp": "123456"}
            return httpx.Response(200, json={"status": "success", "data": {"status": "pending"}})
        raise AssertionError(request.url.path)

    provider = _flw(handler)
    result = await provider.submit_otp("ref-otp", "123456")
    assert calls == ["/v3/transactions/verify_by_reference", "/v3/validate-charge"]
    assert result.status == PaymentStatus.PENDING
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_registry_builds_flutterwave_and_rejects_unknown():
    provider = build_payment_provider(
        "flutterwave",
        {"public_key": "FLWPUBK-x", "secret_key": "FLWSECK-x", "webhook_secret": "wh"},
        callback_url="https://example.com/cb",
    )
    assert isinstance(provider, FlutterwaveProvider)
    assert provider.secret_key == "FLWSECK-x"

    with pytest.raises(ValueError, match="Unsupported payment provider"):
        build_payment_provider("mtn", {})


@pytest.mark.asyncio
async def test_verify_credentials_paystack_ok_and_bad():
    async def ok_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/transaction"
        assert request.headers["Authorization"] == "Bearer sk_live_x"
        return httpx.Response(200, json={"status": True, "data": []})

    p = PaystackProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(ok_handler), base_url="https://api.paystack.co"), secret_key="sk_live_x")
    await p.verify_credentials()  # no raise
    await p._client.aclose()

    async def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Invalid key"})

    p2 = PaystackProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(bad_handler), base_url="https://api.paystack.co"), secret_key="sk_bad")
    with pytest.raises(ValueError, match="Invalid key"):
        await p2.verify_credentials()
    await p2._client.aclose()


@pytest.mark.asyncio
async def test_verify_credentials_flutterwave_ok_and_bad():
    async def ok_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/transactions"
        return httpx.Response(200, json={"status": "success", "data": []})

    p = _flw(ok_handler)
    await p.verify_credentials()
    await p._client.aclose()

    async def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Authorization required"})

    p2 = _flw(bad_handler)
    with pytest.raises(ValueError, match="Authorization required"):
        await p2.verify_credentials()
    await p2._client.aclose()
