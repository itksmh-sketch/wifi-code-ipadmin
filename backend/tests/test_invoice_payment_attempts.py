"""initiate_invoice_payment across attempts: Paystack refuses a reference it has
seen, so a second Pay Now must reuse the open checkout, or open one under a new
reference only when the old one can no longer be paid.

Pure unit tests: Paystack is an httpx.MockTransport, the db a stub.
"""
from __future__ import annotations

import json
import re
import types
import uuid
from decimal import Decimal

import httpx
import pytest

from src.modules.billing import service as billing_service
from src.modules.platform import payment_credentials_service, settings_service

OLD_URL = "https://checkout.paystack.com/old-checkout"
NEW_URL = "https://checkout.paystack.com/new-checkout"
AMOUNT = Decimal("80.00")
PESEWAS = 8000

pytestmark = pytest.mark.asyncio


class Db:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.fixture
def paystack(monkeypatch):
    """Records every call; `verify` is what /transaction/verify answers."""
    calls: list[tuple[str, dict | None]] = []
    state = {"verify": None}

    async def keys(_db):
        return types.SimpleNamespace(is_configured=True, secret_key="sk_live_platform")

    async def setting(_db, key):
        return "https://ip-admin.example.test"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/transaction/verify/"):
            calls.append((path, None))
            answer = state["verify"]
            if isinstance(answer, Exception):
                raise answer
            status, body = answer
            return httpx.Response(status, json=body)
        if path == "/transaction/initialize":
            body = json.loads(request.content)
            calls.append((path, body))
            return httpx.Response(200, json={"status": True, "data": {"authorization_url": NEW_URL}})
        raise AssertionError(f"unexpected Paystack call {path}")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(payment_credentials_service, "resolve_paystack_keys", keys)
    monkeypatch.setattr(settings_service, "get_setting", setting)
    monkeypatch.setattr(billing_service.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return types.SimpleNamespace(calls=calls, state=state)


def _invoice(*, url=None, reference=None):
    return types.SimpleNamespace(id=uuid.uuid4(), amount_ghs=AMOUNT, invoice_number="INV-2026-900",
                                 paystack_payment_url=url, payment_reference=reference)


def _operator():
    return types.SimpleNamespace(id=uuid.uuid4(), contact_email="op@example.test")


def _verified(status, amount=PESEWAS):
    return 200, {"status": True, "data": {"status": status, "amount": amount}}


def _paths(paystack):
    return [path for path, _ in paystack.calls]


async def test_first_attempt_uses_the_invoice_reference_and_stores_it(paystack):
    invoice, db = _invoice(), Db()
    url = await billing_service.initiate_invoice_payment(db, invoice, _operator())
    assert url == NEW_URL
    assert _paths(paystack) == ["/transaction/initialize"], "nothing to verify on a first attempt"
    body = paystack.calls[0][1]
    assert body["reference"] == f"INV-{invoice.id}"
    assert body["amount"] == PESEWAS
    assert body["metadata"]["invoice_id"] == str(invoice.id)
    assert (invoice.paystack_payment_url, invoice.payment_reference) == (NEW_URL, f"INV-{invoice.id}")
    assert db.commits == 1


@pytest.mark.parametrize("status", ["abandoned", "ongoing", "pending", "processing", "queued"])
async def test_an_open_checkout_is_handed_out_again(paystack, status):
    invoice, db = _invoice(url=OLD_URL), Db()
    invoice.payment_reference = f"INV-{invoice.id}-aaaa1111"
    paystack.state["verify"] = _verified(status)
    url = await billing_service.initiate_invoice_payment(db, invoice, _operator())
    assert url == OLD_URL
    assert _paths(paystack) == [f"/transaction/verify/INV-{invoice.id}-aaaa1111"], "no second checkout"
    assert invoice.payment_reference == f"INV-{invoice.id}-aaaa1111"
    assert db.commits == 0


async def test_a_checkout_stored_before_references_were_kept_is_verified_as_inv_id(paystack):
    # INV-2026-005's shape on prod: a URL, no reference.
    invoice = _invoice(url=OLD_URL)
    paystack.state["verify"] = _verified("abandoned")
    assert await billing_service.initiate_invoice_payment(Db(), invoice, _operator()) == OLD_URL
    assert _paths(paystack) == [f"/transaction/verify/INV-{invoice.id}"]


@pytest.mark.parametrize("verify", [
    _verified("failed"),
    _verified("reversed"),
    _verified("abandoned", amount=PESEWAS - 100),   # the invoice amount changed since
    (400, {"status": False, "message": "Transaction reference not found"}),  # opened under other keys
])
async def test_a_dead_checkout_is_replaced_under_a_new_reference(paystack, verify):
    invoice, db = _invoice(url=OLD_URL), Db()
    paystack.state["verify"] = verify
    url = await billing_service.initiate_invoice_payment(db, invoice, _operator())
    assert url == NEW_URL
    assert _paths(paystack) == [f"/transaction/verify/INV-{invoice.id}", "/transaction/initialize"]
    new_ref = paystack.calls[1][1]["reference"]
    assert re.fullmatch(rf"INV-{invoice.id}-[0-9a-f]{{8}}", new_ref), new_ref
    assert (invoice.paystack_payment_url, invoice.payment_reference) == (NEW_URL, new_ref)
    assert paystack.calls[1][1]["metadata"]["invoice_id"] == str(invoice.id)
    assert db.commits == 1


async def test_every_replacement_gets_its_own_reference(paystack):
    invoice = _invoice(url=OLD_URL)
    paystack.state["verify"] = _verified("failed")
    await billing_service.initiate_invoice_payment(Db(), invoice, _operator())
    first = invoice.payment_reference
    await billing_service.initiate_invoice_payment(Db(), invoice, _operator())
    assert invoice.payment_reference != first
    assert _paths(paystack)[2] == f"/transaction/verify/{first}", "the second attempt checks the first's reference"


async def test_a_payment_that_went_through_is_not_charged_again(paystack):
    invoice, db = _invoice(url=OLD_URL), Db()
    paystack.state["verify"] = _verified("success")
    with pytest.raises(ValueError, match="has gone through and is being confirmed"):
        await billing_service.initiate_invoice_payment(db, invoice, _operator())
    assert "/transaction/initialize" not in _paths(paystack)
    assert (invoice.paystack_payment_url, db.commits) == (OLD_URL, 0)


@pytest.mark.parametrize("verify", [httpx.ConnectError("down"), (502, {"status": False, "message": "gateway"})])
async def test_when_paystack_cannot_be_asked_the_open_checkout_is_reused(paystack, verify):
    # Reusing can never open a second charge; guessing "new" could.
    invoice = _invoice(url=OLD_URL)
    paystack.state["verify"] = verify
    assert await billing_service.initiate_invoice_payment(Db(), invoice, _operator()) == OLD_URL
    assert "/transaction/initialize" not in _paths(paystack)


async def test_a_rejected_key_is_an_error_not_a_new_checkout(paystack):
    invoice = _invoice(url=OLD_URL)
    paystack.state["verify"] = (401, {"status": False, "message": "Invalid key"})
    with pytest.raises(ValueError, match="Paystack error: Invalid key"):
        await billing_service.initiate_invoice_payment(Db(), invoice, _operator())
    assert "/transaction/initialize" not in _paths(paystack)
