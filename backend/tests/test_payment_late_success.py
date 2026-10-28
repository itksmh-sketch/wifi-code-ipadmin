"""A payment our two-hour timeout failed may still become a sale; one the
provider failed may not. No database: the row lock, the 48-hour window and the
re-check job's selection are covered against Postgres in
test_payment_late_success_flow.py."""
import uuid
from decimal import Decimal

import pytest

from src.db.models import PaymentTransaction, Plan, Voucher
from src.modules.payments import operator_alerts
from src.modules.payments.providers.base import provider_unreachable_result
from src.modules.payments.service import PaymentService
from src.modules.payments.types import (
    TIMEOUT_FAILURE_REASON,
    PaymentMethod,
    PaymentStatus,
    PaymentVerificationResult,
)
from test_payment_service import FakeDb


def _failed_tx(reason: str, phone: str | None = "233551234987") -> PaymentTransaction:
    return PaymentTransaction(
        id=uuid.uuid4(), internal_reference=f"ref-late-{uuid.uuid4().hex[:6]}", status=PaymentStatus.FAILED.value,
        failure_reason=reason, plan_id=uuid.uuid4(), site_id=uuid.uuid4(), amount_ghs=Decimal("4.00"),
        payment_method=PaymentMethod.MTN_MOMO.value, provider="paystack", provider_reference="charge-ref",
        phone_number=phone, is_diagnostic=False,
    )


def _plan(tx) -> Plan:
    return Plan(id=tx.plan_id, name="1 hour", type="time", duration_minutes=60, data_limit_mb=None,
                download_speed_kbps=1024, upload_speed_kbps=512, price_ghs=Decimal("4.00"))


def _result(status: str, reason: str | None = None) -> PaymentVerificationResult:
    return PaymentVerificationResult(status=PaymentStatus(status), amount_ghs=Decimal("4.00"), failure_reason=reason,
                                     provider_state=status)


class _Db(FakeDb):
    def __init__(self, results=()):
        super().__init__(results)
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.fixture
def alerts(monkeypatch):
    sent = {"late": [], "contradicted": []}

    async def late(payment_id):
        sent["late"].append(payment_id)

    async def contradicted(payment_id):
        sent["contradicted"].append(payment_id)

    monkeypatch.setattr(operator_alerts, "notify_late_success", late)
    monkeypatch.setattr(operator_alerts, "notify_success_after_failure", contradicted)
    return sent


def test_the_timeout_marker_is_the_value_the_job_has_always_written():
    # Rows timed out before this change carry it; they must still match.
    assert TIMEOUT_FAILURE_REASON == "timeout"


# ── resolve_successful_payment ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_timed_out_payment_confirmed_later_becomes_a_sale(request, alerts):
    tx = _failed_tx(TIMEOUT_FAILURE_REASON)
    db = _Db([tx, _plan(tx)])

    out = await PaymentService().resolve_successful_payment(db, internal_reference=tx.internal_reference,
                                                            trigger_source="poll")

    assert out.status == PaymentStatus.SUCCESS.value and out.failure_reason is None
    assert [v for v in db.added if isinstance(v, Voucher)] and out.voucher_id is not None
    assert request.node.voucher_sms_enqueued == [tx.id]
    assert alerts == {"late": [tx.id], "contradicted": []}


@pytest.mark.asyncio
async def test_a_payment_the_provider_failed_is_not_revived_and_the_operator_is_asked_to_check(request, alerts):
    tx = _failed_tx("Insufficient funds")
    db = _Db([tx])

    out = await PaymentService().resolve_successful_payment(db, internal_reference=tx.internal_reference,
                                                            trigger_source="webhook")

    assert out.status == PaymentStatus.FAILED.value and out.failure_reason == "Insufficient funds"
    assert not [v for v in db.added if isinstance(v, Voucher)] and out.voucher_id is None
    assert request.node.voucher_sms_enqueued == []
    assert alerts == {"late": [], "contradicted": [tx.id]}


@pytest.mark.asyncio
async def test_an_ordinary_success_sends_no_operator_alert(request, alerts):
    tx = _failed_tx(TIMEOUT_FAILURE_REASON)
    tx.status, tx.failure_reason = PaymentStatus.PENDING.value, None
    out = await PaymentService().resolve_successful_payment(_Db([tx, _plan(tx)]),
                                                            internal_reference=tx.internal_reference,
                                                            trigger_source="webhook")
    assert out.status == PaymentStatus.SUCCESS.value
    assert alerts == {"late": [], "contradicted": []}


# ── apply_late_result ─────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["pending", "unreachable"])
async def test_no_verdict_keeps_it_failed_by_timeout(answer, alerts):
    tx = _failed_tx(TIMEOUT_FAILURE_REASON)
    result = _result("pending") if answer == "pending" else provider_unreachable_result("charge-ref")

    out = await PaymentService().apply_late_result(_Db(), tx=tx, result=result, trigger_source="poll")

    assert out.status == PaymentStatus.FAILED.value and out.failure_reason == TIMEOUT_FAILURE_REASON
    assert out.last_status_check_at is not None
    assert alerts == {"late": [], "contradicted": []}


@pytest.mark.asyncio
async def test_a_provider_failure_replaces_our_reason(monkeypatch):
    from src.modules.audit import stored_text

    async def as_is(db, operator_id, text):
        return text

    monkeypatch.setattr(stored_text, "operator_payment_error", as_is)
    tx = _failed_tx(TIMEOUT_FAILURE_REASON)
    out = await PaymentService().apply_late_result(_Db(), tx=tx, result=_result("failed", "Declined"),
                                                   trigger_source="poll")
    assert out.status == PaymentStatus.FAILED.value and out.failure_reason == "Declined"


@pytest.mark.asyncio
async def test_a_provider_failure_without_a_reason_still_ends_the_checks():
    tx = _failed_tx(TIMEOUT_FAILURE_REASON)
    out = await PaymentService().apply_late_result(_Db(), tx=tx, result=_result("failed"), trigger_source="poll")
    assert out.failure_reason == "provider_failed"


@pytest.mark.asyncio
async def test_a_provider_success_resolves_it(alerts):
    tx = _failed_tx(TIMEOUT_FAILURE_REASON)
    out = await PaymentService().apply_late_result(_Db([tx, _plan(tx)]), tx=tx, result=_result("success"),
                                                   trigger_source="poll")
    assert out.status == PaymentStatus.SUCCESS.value and alerts["late"] == [tx.id]


# ── refresh_transaction_status ────────────────────────────────────────────

class _Provider:
    def __init__(self, result):
        self.result, self.calls = result, 0

    async def verify(self, *args, **kwargs):
        self.calls += 1
        return self.result


def _with_provider(monkeypatch, service, provider):
    async def lookup(db, tx):
        return provider

    monkeypatch.setattr(service, "provider_for_transaction", lookup)


@pytest.mark.asyncio
async def test_the_portal_poll_never_asks_about_a_timed_out_payment(monkeypatch):
    service, provider = PaymentService(), _Provider(_result("success"))
    _with_provider(monkeypatch, service, provider)
    tx = _failed_tx(TIMEOUT_FAILURE_REASON)
    out = await service.refresh_transaction_status(_Db(), tx=tx, force=False)
    assert provider.calls == 0 and out.status == PaymentStatus.FAILED.value


@pytest.mark.asyncio
async def test_a_forced_re_check_asks_about_a_timed_out_payment_and_never_revives_it_to_pending(monkeypatch):
    service, provider = PaymentService(), _Provider(_result("pending"))
    _with_provider(monkeypatch, service, provider)
    tx = _failed_tx(TIMEOUT_FAILURE_REASON)
    out = await service.refresh_transaction_status(_Db(), tx=tx, force=True)
    assert provider.calls == 1
    assert out.status == PaymentStatus.FAILED.value and out.failure_reason == TIMEOUT_FAILURE_REASON


@pytest.mark.asyncio
async def test_a_forced_re_check_never_asks_about_a_payment_the_provider_failed(monkeypatch):
    service, provider = PaymentService(), _Provider(_result("success"))
    _with_provider(monkeypatch, service, provider)
    tx = _failed_tx("Insufficient funds")
    out = await service.refresh_transaction_status(_Db(), tx=tx, force=True)
    assert provider.calls == 0 and out.status == PaymentStatus.FAILED.value


# ── the email's wording ───────────────────────────────────────────────────

_FACTS = {"to": "ops@example.test", "isp_name": "Osu Net", "amount": "GHS 4.00", "plan": "1 hour",
          "reference": "INT-abc", "started": "28 Oct 2026, 14:02 GMT", "code_suffix": "…WXYZ",
          "has_phone": True, "failure_reason": "Insufficient funds", "platform_name": "IpAdmin"}


def test_the_late_success_email_with_and_without_a_phone():
    subject, paragraphs = operator_alerts._late_success(_FACTS)
    text = "\n".join(paragraphs)
    assert subject == "Payment confirmed late: GHS 4.00"
    assert "INT-abc" in text and "…WXYZ" in text and "by SMS" in text
    _, paragraphs = operator_alerts._late_success({**_FACTS, "has_phone": False})
    assert "no phone number" in "\n".join(paragraphs)


def test_the_contradiction_email_says_nothing_was_issued():
    subject, paragraphs = operator_alerts._success_after_failure({**_FACTS, "code_suffix": ""})
    text = "\n".join(paragraphs)
    assert "No voucher was issued" in text and "Insufficient funds" in text and "Voucher:" not in text
