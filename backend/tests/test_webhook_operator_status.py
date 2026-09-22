"""Unit tests for the operator lookup in the payment-webhook handler.

Regression cover for the suspended-operator webhook bug: the lookup used to
filter ``ISPOperator.status == "approved"``, so a webhook for a transaction
that a now-suspended operator's portal had already accepted 404'd. The
provider retried into that 404 and gave up, and the customer who had already
paid never got a voucher. Completing an in-flight payment must not depend on
the operator's *current* status — suspension is enforced where a purchase
starts, not where a paid one finishes.

Pure unit tests: no server, no DB, no Redis. Deliberately defines neither
``BASE_URL`` nor ``_request``, so tests/conftest.py correctly classifies this
module as unit-only.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from src.db.models import ISPOperator, OperatorPaymentCredential
from src.modules.payments.types import PaymentStatus
from src.modules.webhooks import routes as webhook_routes


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeDb:
    """Returns the queued rows in order: operator lookup, then credentials."""

    def __init__(self, results):
        self.results = list(results)
        self.executed = []

    async def execute(self, statement):
        self.executed.append(statement)
        return _ScalarResult(self.results.pop(0) if self.results else None)


class FakeRequest:
    def __init__(self, body: bytes = b'{"event":"charge.success"}'):
        self._body = body
        self.headers = {"x-paystack-signature": "sig"}
        self.client = type("C", (), {"host": "1.2.3.4"})()

    async def body(self) -> bytes:
        return self._body


class FakeRedis:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, *args):
        self.jobs.append(args)


class _ParsedWebhook:
    internal_reference = "ref-in-flight"
    status = PaymentStatus.SUCCESS
    provider_reference = "prov-123"


class _StubProvider:
    async def handle_webhook(self, headers, raw_body):
        return _ParsedWebhook()


@pytest.fixture
def patched(monkeypatch):
    """Stub out everything around the lookup under test."""
    redis = FakeRedis()

    async def _no_rate_limit(*args, **kwargs):
        return None

    async def _redis_pool():
        return redis

    monkeypatch.setattr(webhook_routes, "enforce_rate_limit", _no_rate_limit)
    monkeypatch.setattr(webhook_routes, "get_redis_pool", _redis_pool)
    monkeypatch.setattr(webhook_routes, "load_credentials", lambda creds: {})
    monkeypatch.setattr(
        webhook_routes, "build_payment_provider", lambda key, creds, callback_url=None: _StubProvider()
    )
    return redis


def _operator(status: str) -> ISPOperator:
    return ISPOperator(
        id=uuid.uuid4(),
        name="Osu Hotspot",
        slug="osu-hotspot",
        contact_email="ops@example.test",
        status=status,
    )


def _credentials(operator_id) -> OperatorPaymentCredential:
    return OperatorPaymentCredential(
        id=uuid.uuid4(),
        isp_operator_id=operator_id,
        provider="paystack",
        is_active=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["approved", "suspended", "pending", "cancelled"])
async def test_webhook_accepted_regardless_of_operator_status(patched, status):
    """The money already moved — status must not gate completing the payment."""
    operator = _operator(status)
    db = FakeDb([operator, _credentials(operator.id)])

    result = await webhook_routes._process_webhook("paystack", operator.slug, FakeRequest(), db)

    assert result == {"ok": True}
    assert len(patched.jobs) == 1
    job = patched.jobs[0]
    assert job[0] == "process_webhook_event"
    assert job[1] == "paystack"
    assert job[2] == "ref-in-flight"


@pytest.mark.asyncio
async def test_webhook_for_unknown_operator_still_404s(patched):
    """The operator must still exist — only the status filter was dropped."""
    db = FakeDb([None])

    with pytest.raises(HTTPException) as exc:
        await webhook_routes._process_webhook("paystack", "no-such-operator", FakeRequest(), db)

    assert exc.value.status_code == 404
    assert exc.value.detail == "Unknown operator"
    assert patched.jobs == []
    # Bailed before the credentials lookup.
    assert len(db.executed) == 1


@pytest.mark.asyncio
async def test_webhook_still_requires_active_payment_credentials(patched):
    """Dropping the status filter must not loosen the credentials requirement."""
    operator = _operator("suspended")
    db = FakeDb([operator, None])

    with pytest.raises(HTTPException) as exc:
        await webhook_routes._process_webhook("paystack", operator.slug, FakeRequest(), db)

    assert exc.value.status_code == 404
    assert exc.value.detail == "Payment credentials are not configured"
    assert patched.jobs == []


@pytest.mark.asyncio
async def test_operator_lookup_does_not_filter_on_status(patched):
    """Guards the specific regression: no status predicate in the WHERE clause."""
    operator = _operator("suspended")
    db = FakeDb([operator, _credentials(operator.id)])

    await webhook_routes._process_webhook("paystack", operator.slug, FakeRequest(), db)

    # Only the WHERE clause matters — `status` is naturally in the SELECT list.
    where_clause = str(db.executed[0].whereclause)
    assert "isp_operators.slug" in where_clause
    assert "status" not in where_clause
