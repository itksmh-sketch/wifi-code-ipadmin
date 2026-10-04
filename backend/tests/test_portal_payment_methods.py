"""Captive-portal purchases are mobile money only; card stays for platform
invoice payment.

A card payment can't finish from behind the hotspot (the walled garden blocks
the provider's checkout and 3-D Secure, and the return journey isn't built),
so the portal refuses it before creating anything. The provider's card code
stays in place, unreachable from the portal.

Pure unit tests: the database, the payment service and Paystack are faked. No
server, no DB, no Redis.
"""
from __future__ import annotations

import re
import types
import uuid
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from src.app import app
from src.db.base import get_db
from src.modules.billing import service as billing_service
from src.modules.payments.dependencies import get_payment_service
from src.modules.platform import payment_credentials_service, settings_service
from src.portal import routes as portal_routes

PAY_HTML = Path(__file__).resolve().parents[2] / "portal" / "pay.html"
OPERATOR_ID = uuid.uuid4()
SITE_ID = uuid.uuid4()
PLAN_ID = uuid.uuid4()


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeDb:
    """Answers the portal's two lookups in order: the operator, then the plan."""

    def __init__(self):
        operator = types.SimpleNamespace(id=OPERATOR_ID, status="approved")
        plan = types.SimpleNamespace(id=PLAN_ID, site_id=SITE_ID, price_ghs=Decimal("2.00"))
        self.results = [operator, plan]
        self.queries = 0

    async def execute(self, _stmt):
        self.queries += 1
        return _Result(self.results.pop(0) if self.results else None)


class FakePaymentService:
    def __init__(self):
        self.created = []
        self.initiated = []

    async def create_pending_transaction(self, db, **kwargs):
        self.created.append(kwargs)
        return types.SimpleNamespace(id=uuid.uuid4())

    async def initiate_payment(self, db, *, transaction_id):
        self.initiated.append(transaction_id)
        return types.SimpleNamespace(
            internal_reference=str(uuid.uuid4()), status="pending", next_action="wait",
            display_message="Approve the prompt on your phone.", payment_channel="mobile_money",
            provider_payload={},
        )


@pytest.fixture
def portal(monkeypatch):
    db, service = FakeDb(), FakePaymentService()

    async def no_limit(*args, **kwargs):
        return None

    async def resolve(_db, _rt, _gateway):
        return OPERATOR_ID, SITE_ID

    monkeypatch.setattr(portal_routes, "enforce_rate_limit", no_limit)
    monkeypatch.setattr(portal_routes, "_resolve_operator", resolve)

    async def fake_db():
        yield db

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_payment_service] = lambda: service
    yield db, service
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_payment_service, None)


async def initiate(method, phone="0244123456"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/portal/initiate-payment", json={
            "plan_id": str(PLAN_ID), "site_id": str(SITE_ID), "gateway": "10.5.50.1",
            "phone": phone, "payment_method": method,
        })


# ── Server side ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("phone", ["0244123456", None])
async def test_a_portal_card_request_is_refused_before_anything_is_created(portal, phone):
    db, service = portal
    res = await initiate("card", phone=phone)
    assert res.status_code == 400
    assert res.json()["detail"] == portal_routes.PORTAL_CARD_REFUSED
    assert "mobile money" in res.json()["detail"]
    assert db.queries == 0, "refused before any lookup"
    assert service.created == [] and service.initiated == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["mtn_momo", "vodafone_cash", "airteltigo"])
async def test_a_portal_momo_request_still_starts_a_payment(portal, method):
    _, service = portal
    res = await initiate(method)
    assert res.status_code == 200, res.text
    assert res.json()["next_action"] == "wait"
    assert [c["payment_method"].value for c in service.created] == [method]
    assert len(service.initiated) == 1


# ── The page ──────────────────────────────────────────────────────────────


def test_pay_page_offers_mobile_money_only():
    html = PAY_HTML.read_text()
    methods = re.findall(r'name="method" value="([^"]+)"', html)
    assert methods == ["mtn_momo", "vodafone_cash", "airteltigo"]
    # Nothing card-specific left behind (".card"/"planCard" are layout classes).
    for leftover in ('value="card"', '=== "card"', "syncPhoneRequirement", "Not required for card", "card or mobile money"):
        assert leftover not in html, leftover
    assert re.search(r'<input type="tel" id="phone" name="phone"[^>]*\brequired\b', html)


# ── Platform invoice payment keeps card ───────────────────────────────────


@pytest.mark.asyncio
async def test_invoice_payment_is_unaffected_and_does_not_restrict_channels(monkeypatch):
    seen = {}

    async def keys(_db):
        return types.SimpleNamespace(is_configured=True, secret_key="sk_test_platform")

    async def setting(_db, key):
        assert key == "platform_app_url"
        return "https://ip-admin.example.test"

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": True, "data": {"authorization_url": "https://checkout.paystack.com/inv"}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(payment_credentials_service, "resolve_paystack_keys", keys)
    monkeypatch.setattr(settings_service, "get_setting", setting)
    monkeypatch.setattr(billing_service.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    class Db:
        async def commit(self):
            return None

    invoice = types.SimpleNamespace(id=uuid.uuid4(), amount_ghs=Decimal("150.00"), invoice_number="INV-1",
                                    paystack_payment_url=None)
    operator = types.SimpleNamespace(id=uuid.uuid4(), contact_email="op@example.test")

    url = await billing_service.initiate_invoice_payment(Db(), invoice, operator)

    assert url == "https://checkout.paystack.com/inv"
    assert seen["path"] == "/transaction/initialize"
    assert "channels" not in seen["body"], "card stays available on the invoice checkout"
    assert seen["body"]["callback_url"] == "https://ip-admin.example.test/api/v1/billing/payment-callback"
