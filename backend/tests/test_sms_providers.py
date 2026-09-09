"""SMS provider unit tests — recorded-fixture style, no network.

Mirrors tests/test_payment_providers.py. Covers the explicit-credential
constructors, the request shape each provider sends, and — the point of the
Phase 2 error audit — that every failure mode yields an informative
SMSSendResult.error, not just an HTTP status code.
"""
import base64
import json

import httpx
import pytest

from src.modules.sms.providers.africastalking import AfricasTalkingSMSProvider
from src.modules.sms.providers.hubtel import HubtelSMSProvider
from src.modules.sms.providers.registry import build_sms_provider


def _hubtel(handler) -> HubtelSMSProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://smsc.hubtel.com")
    return HubtelSMSProvider(client_id="cid", client_secret="csec", sender_id="MyISP", client=client)


def _at(handler) -> AfricasTalkingSMSProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.africastalking.com")
    return AfricasTalkingSMSProvider(api_key="key-123", username="myapp", sender_id="MyISP", client=client)


# ---------------------------------------------------------------------------
# Hubtel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hubtel_send_ok_request_shape_and_reference():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "status": 0, "messageId": "eadbfcd0-1", "rate": 0.03,
            "statusDescription": "request submitted successfully",
        })

    result = await _hubtel(handler).send("233551234987", "hello")

    assert seen["path"] == "/v1/messages/send"
    assert seen["auth"] == "Basic " + base64.b64encode(b"cid:csec").decode()
    assert seen["body"] == {"From": "MyISP", "To": "233551234987", "Content": "hello"}
    assert result.success is True
    assert result.provider_reference == "eadbfcd0-1"


@pytest.mark.asyncio
async def test_hubtel_http_error_carries_response_reason():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Client credentials are invalid"})

    result = await _hubtel(handler).send("233551234987", "hi")
    assert result.success is False
    assert "hubtel_http_401" in result.error
    assert "Client credentials are invalid" in result.error


@pytest.mark.asyncio
async def test_hubtel_accepted_http_but_rejected_status_is_a_failure():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 4001, "statusDescription": "Invalid Recipient"})

    result = await _hubtel(handler).send("bad", "hi")
    assert result.success is False
    assert "hubtel_status_4001" in result.error
    assert "Invalid Recipient" in result.error


@pytest.mark.asyncio
async def test_hubtel_not_configured_makes_no_call():
    called = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://smsc.hubtel.com")
    provider = HubtelSMSProvider(client_id="cid", client_secret="", sender_id="MyISP", client=client)
    result = await provider.send("233551234987", "hi")
    assert result == type(result)(success=False, error="hubtel_not_configured")
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Africa's Talking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_africastalking_send_ok_request_shape_and_reference():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["apikey"] = request.headers.get("apiKey")
        seen["body"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(201, json={"SMSMessageData": {
            "Message": "Sent to 1/1 Total Cost: KES 0.8000",
            "Recipients": [{"statusCode": 101, "status": "Success", "messageId": "ATXid_9"}],
        }})

    result = await _at(handler).send("+233551234987", "hello")

    assert seen["path"] == "/version1/messaging"
    assert seen["apikey"] == "key-123"
    assert seen["body"] == {"username": "myapp", "to": "+233551234987", "message": "hello", "from": "MyISP"}
    assert result.success is True
    assert result.provider_reference == "ATXid_9"


@pytest.mark.asyncio
async def test_africastalking_recipient_rejected_is_a_failure_with_reason():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"SMSMessageData": {
            "Message": "Sent to 0/1",
            "Recipients": [{"statusCode": 405, "status": "InsufficientBalance"}],
        }})

    result = await _at(handler).send("+233551234987", "hi")
    assert result.success is False
    assert "InsufficientBalance" in result.error


@pytest.mark.asyncio
async def test_africastalking_http_error_carries_response_reason():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Application not found or credentials are invalid")

    result = await _at(handler).send("+233551234987", "hi")
    assert result.success is False
    assert "africastalking_http_401" in result.error
    assert "credentials are invalid" in result.error


@pytest.mark.asyncio
async def test_africastalking_not_configured_makes_no_call():
    called = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(201, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.africastalking.com")
    provider = AfricasTalkingSMSProvider(api_key="", username="myapp", sender_id="MyISP", client=client)
    result = await provider.send("+233551234987", "hi")
    assert result.success is False and result.error == "africastalking_not_configured"
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_build_sms_provider_maps_keys_and_rejects_unknown():
    h = build_sms_provider("hubtel", {"client_id": "a", "client_secret": "b", "from": "SID"})
    assert isinstance(h, HubtelSMSProvider) and h.sender_id == "SID"

    a = build_sms_provider("africastalking", {"api_key": "k", "username": "u", "from": "SID"})
    assert isinstance(a, AfricasTalkingSMSProvider) and a.username == "u" and a.sender_id == "SID"

    with pytest.raises(ValueError, match="Unsupported SMS provider"):
        build_sms_provider("twilio", {})
