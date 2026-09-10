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
from src.modules.sms.providers.arkesel import ArkeselSMSProvider
from src.modules.sms.providers.hubtel import HubtelSMSProvider
from src.modules.sms.providers.registry import build_sms_provider


def _hubtel(handler) -> HubtelSMSProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://smsc.hubtel.com")
    return HubtelSMSProvider(client_id="cid", client_secret="csec", sender_id="MyISP", client=client)


def _at(handler) -> AfricasTalkingSMSProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.africastalking.com")
    return AfricasTalkingSMSProvider(api_key="key-123", username="myapp", sender_id="MyISP", client=client)


def _ark(handler) -> ArkeselSMSProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://sms.arkesel.com")
    return ArkeselSMSProvider(api_key="key-123", sender_id="MyISP", client=client)


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
async def test_africastalking_empty_recipients_is_a_failure_with_reason():
    # AT's account-level rejection shape (most often an unregistered/unapproved
    # sender ID): HTTP 201, but "Sent to 0/1" and an empty Recipients array -
    # nothing was queued. Must NOT be reported as a successful send.
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"SMSMessageData": {
            "Message": "Sent to 0/1 Total Cost: 0",
            "Recipients": [],
        }})

    result = await _at(handler).send("+233551234987", "hi")
    assert result.success is False
    assert "no_recipients" in result.error
    assert "Sent to 0/1" in result.error


@pytest.mark.asyncio
async def test_africastalking_2xx_without_smsmessagedata_is_a_failure():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={})

    result = await _at(handler).send("+233551234987", "hi")
    assert result.success is False
    assert "no_recipients" in result.error


@pytest.mark.asyncio
async def test_africastalking_normalizes_bare_ghana_msisdn_to_e164():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["to"] = dict(httpx.QueryParams(request.content.decode()))["to"]
        return httpx.Response(201, json={"SMSMessageData": {
            "Message": "Sent to 1/1 Total Cost: GHS 0.0350",
            "Recipients": [{"statusCode": 101, "status": "Success", "messageId": "ATXid_1"}],
        }})

    # upstream (payments.service.normalize_phone) hands us the bare 233… form
    result = await _at(handler).send("233549053851", "hi")
    assert seen["to"] == "+233549053851"
    assert result.success is True

    result = await _at(handler).send("0549053851", "hi")  # local form
    assert seen["to"] == "+233549053851"


@pytest.mark.asyncio
async def test_africastalking_http_error_carries_response_reason():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Application not found or credentials are invalid")

    result = await _at(handler).send("+233551234987", "hi")
    assert result.success is False
    assert "africastalking_http_401" in result.error
    assert "credentials are invalid" in result.error


@pytest.mark.asyncio
async def test_africastalking_verify_credentials_ok():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["username"] = request.url.params.get("username")
        seen["apikey"] = request.headers.get("apiKey")
        return httpx.Response(200, json={"UserData": {"balance": "KES 1,234.5"}})

    detail = await _at(handler).verify_credentials()  # must not raise
    assert seen["path"] == "/version1/user"
    assert seen["username"] == "myapp"
    assert seen["apikey"] == "key-123"
    assert detail == "balance KES 1,234.5"


@pytest.mark.asyncio
async def test_africastalking_verify_credentials_ok_without_balance_returns_none():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"UserData": {}})

    assert await _at(handler).verify_credentials() is None


@pytest.mark.asyncio
async def test_africastalking_verify_credentials_rejects_bad_key():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Application not found or credentials are invalid")

    with pytest.raises(ValueError, match="credentials are invalid"):
        await _at(handler).verify_credentials()


@pytest.mark.asyncio
async def test_africastalking_verify_credentials_extracts_errorMessage():
    # AT's real 401 body shape (confirmed against the live API).
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errorMessage": "The supplied authentication is invalid"})

    with pytest.raises(ValueError, match="The supplied authentication is invalid"):
        await _at(handler).verify_credentials()


@pytest.mark.asyncio
async def test_hubtel_verify_credentials_not_implemented():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with pytest.raises(NotImplementedError):
        await _hubtel(handler).verify_credentials()


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
# Arkesel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_arkesel_send_ok_request_shape_and_reference():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["apikey"] = request.headers.get("api-key")
        seen["body"] = json.loads(request.content.decode())
        # Exact body captured from a live sandbox send (2026-09-10): `data` is a
        # LIST of {id, recipient}; balance echoed at top level; no credits field.
        return httpx.Response(200, json={
            "data": [{"id": "c2bda09f-9754-4b3f-8f2d-9d02d0e42b06", "recipient": "233549053851"}],
            "status": "success",
            "main_balance": 0.2,
            "sms_balance": 5,
        })

    result = await _ark(handler).send("233549053851", "hello")

    assert seen["path"] == "/api/v2/sms/send"
    assert seen["apikey"] == "key-123"
    assert seen["body"] == {"sender": "MyISP", "message": "hello", "recipients": ["233549053851"]}
    assert result.success is True
    assert result.provider_reference == "c2bda09f-9754-4b3f-8f2d-9d02d0e42b06"


@pytest.mark.asyncio
async def test_arkesel_normalizes_local_and_plus_prefixed_msisdn():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["recipients"] = json.loads(request.content.decode())["recipients"]
        return httpx.Response(200, json={"status": "success", "data": {"id": "x"}})

    await _ark(handler).send("0549053851", "hi")
    assert seen["recipients"] == ["233549053851"]
    await _ark(handler).send("+233 54 905 3851", "hi")
    assert seen["recipients"] == ["233549053851"]


@pytest.mark.asyncio
async def test_arkesel_extracts_id_from_alternate_data_shapes():
    for data in ({"0": {"recipient": "233549053851", "id": "keyed"}}, [{"recipient": "x", "id": "listed"}]):
        async def handler(request, _data=data) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "data": _data})

        result = await _ark(handler).send("233549053851", "hi")
        assert result.success is True
        assert result.provider_reference in ("keyed", "listed")


@pytest.mark.asyncio
async def test_arkesel_http_error_carries_response_reason():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"status": "error", "message": "Invalid phone number"})

    result = await _ark(handler).send("bad", "hi")
    assert result.success is False
    assert "arkesel_http_422" in result.error
    assert "Invalid phone number" in result.error


@pytest.mark.asyncio
async def test_arkesel_2xx_with_error_status_is_a_failure_with_reason():
    # The Africa's Talking lesson: a 2xx alone is not success. Arkesel signals
    # rejection (e.g. insufficient balance) in the body via `status`.
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "error", "message": "Insufficient balance"})

    result = await _ark(handler).send("233549053851", "hi")
    assert result.success is False
    assert "arkesel_status_error" in result.error
    assert "Insufficient balance" in result.error


@pytest.mark.asyncio
async def test_arkesel_2xx_unparseable_body_is_a_failure():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    result = await _ark(handler).send("233549053851", "hi")
    assert result.success is False
    assert "arkesel_status_unknown" in result.error


@pytest.mark.asyncio
async def test_arkesel_not_configured_makes_no_call():
    called = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"status": "success"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://sms.arkesel.com")
    provider = ArkeselSMSProvider(api_key="key", sender_id="", client=client)
    result = await provider.send("233549053851", "hi")
    assert result.success is False and result.error == "arkesel_not_configured"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_arkesel_verify_credentials_ok_returns_balance():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["apikey"] = request.headers.get("api-key")
        # Exact body captured from the live account (2026-09-10).
        return httpx.Response(200, json={
            "data": {"sms_balance": 5, "main_balance": "GHS 0.2"}, "status": "success",
        })

    detail = await _ark(handler).verify_credentials()
    assert seen["path"] == "/api/v2/clients/balance-details"
    assert seen["apikey"] == "key-123"
    assert detail == "balance GHS 0.2"


@pytest.mark.asyncio
async def test_arkesel_verify_credentials_ok_without_balance_returns_none():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {}})

    assert await _ark(handler).verify_credentials() is None


@pytest.mark.asyncio
async def test_arkesel_verify_credentials_rejects_bad_key():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Invalid API Key"})

    with pytest.raises(ValueError, match="Invalid API Key"):
        await _ark(handler).verify_credentials()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_build_sms_provider_maps_keys_and_rejects_unknown():
    h = build_sms_provider("hubtel", {"client_id": "a", "client_secret": "b", "from": "SID"})
    assert isinstance(h, HubtelSMSProvider) and h.sender_id == "SID"

    a = build_sms_provider("africastalking", {"api_key": "k", "username": "u", "from": "SID"})
    assert isinstance(a, AfricasTalkingSMSProvider) and a.username == "u" and a.sender_id == "SID"

    k = build_sms_provider("arkesel", {"api_key": "k", "from": "SID"})
    assert isinstance(k, ArkeselSMSProvider) and k.sender_id == "SID"

    with pytest.raises(ValueError, match="Unsupported SMS provider"):
        build_sms_provider("twilio", {})
