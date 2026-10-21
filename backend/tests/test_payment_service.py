import uuid
from decimal import Decimal

import pytest

from src.db.models import PaymentTransaction, Plan, Voucher
from src.modules.payments.service import PaymentService
from src.modules.payments.types import (
    PaymentInitiationResult,
    PaymentMethod,
    PaymentNextAction,
    PaymentStatus,
)


class DummyProvider:
    async def initiate(self, amount_ghs, phone, plan_id, site_id, internal_reference, payment_method):
        return PaymentInitiationResult(
            provider_reference=f"prov-{internal_reference}",
            status=PaymentStatus.PENDING,
            next_action=PaymentNextAction.WAIT,
        )

    async def verify(self, provider_reference):
        raise NotImplementedError

    async def handle_webhook(self, headers, raw_body):
        raise NotImplementedError


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _BeginCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeDb:
    def __init__(self, results):
        self.results = list(results)
        self.executed = []
        self.added = []

    def in_transaction(self) -> bool:
        return False

    def begin(self):
        return _BeginCtx()

    async def execute(self, statement):
        self.executed.append(statement)
        value = self.results.pop(0)
        return _ScalarResult(value)

    def add(self, obj):
        if isinstance(obj, Voucher) and not obj.id:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    async def flush(self):
        return None

    async def refresh(self, _):
        return None


def _service() -> PaymentService:
    return PaymentService()


@pytest.mark.asyncio
async def test_resolve_successful_payment_uses_select_for_update_and_generates_voucher():
    tx = PaymentTransaction(
        id=uuid.uuid4(),
        internal_reference="ref-1",
        status=PaymentStatus.PENDING.value,
        plan_id=uuid.uuid4(),
        site_id=uuid.uuid4(),
        amount_ghs=Decimal("4.00"),
        payment_method=PaymentMethod.MTN_MOMO.value,
        provider="mtn",
    )
    plan = Plan(
        id=tx.plan_id,
        name="1 hour",
        type="time",
        duration_minutes=60,
        data_limit_mb=None,
        download_speed_kbps=1024,
        upload_speed_kbps=512,
        price_ghs=Decimal("4.00"),
    )
    db = FakeDb([tx, plan])
    service = _service()

    updated = await service.resolve_successful_payment(
        db,
        internal_reference="ref-1",
        trigger_source="webhook",
    )

    assert updated.status == PaymentStatus.SUCCESS.value
    assert updated.voucher_id is not None
    assert len(db.executed) >= 1
    assert getattr(db.executed[0], "_for_update_arg", None) is not None


def _paid_tx_and_plan(phone: str | None):
    tx = PaymentTransaction(
        id=uuid.uuid4(),
        internal_reference=f"ref-sms-{uuid.uuid4().hex[:6]}",
        status=PaymentStatus.PENDING.value,
        plan_id=uuid.uuid4(),
        site_id=uuid.uuid4(),
        amount_ghs=Decimal("4.00"),
        payment_method=PaymentMethod.MTN_MOMO.value,
        provider="mtn",
        phone_number=phone,
    )
    plan = Plan(
        id=tx.plan_id, name="1 hour", type="time", duration_minutes=60,
        data_limit_mb=None, download_speed_kbps=1024, upload_speed_kbps=512,
        price_ghs=Decimal("4.00"),
    )
    return tx, plan


@pytest.mark.asyncio
async def test_successful_payment_enqueues_voucher_sms_instead_of_sending(request, monkeypatch):
    tx, plan = _paid_tx_and_plan("233551234987")
    monkeypatch.setattr(
        "src.modules.sms.voucher_delivery.build_sms_provider",
        lambda *a, **k: pytest.fail("the request path must not send the SMS itself"),
    )

    updated = await _service().resolve_successful_payment(
        FakeDb([tx, plan]), internal_reference=tx.internal_reference, trigger_source="webhook"
    )
    assert updated.status == PaymentStatus.SUCCESS.value
    assert request.node.voucher_sms_enqueued == [tx.id]


@pytest.mark.asyncio
async def test_no_voucher_sms_enqueued_without_a_phone_number(request):
    tx, plan = _paid_tx_and_plan(None)

    updated = await _service().resolve_successful_payment(
        FakeDb([tx, plan]), internal_reference=tx.internal_reference, trigger_source="webhook"
    )
    assert updated.status == PaymentStatus.SUCCESS.value
    assert request.node.voucher_sms_enqueued == []


@pytest.mark.asyncio
async def test_enqueue_failure_never_fails_the_payment(monkeypatch):
    tx, plan = _paid_tx_and_plan("233551234987")

    async def _redis_down():
        raise ConnectionError("redis down")

    # The real enqueue helper, with Redis unreachable.
    from src.jobs import voucher_sms
    monkeypatch.setattr(voucher_sms, "get_redis_pool", _redis_down)
    monkeypatch.setattr("src.modules.payments.service.enqueue_voucher_sms", voucher_sms.enqueue_voucher_sms)

    updated = await _service().resolve_successful_payment(
        FakeDb([tx, plan]), internal_reference=tx.internal_reference, trigger_source="portal_poll"
    )
    assert updated.status == PaymentStatus.SUCCESS.value
    assert updated.voucher_id is not None


@pytest.mark.asyncio
async def test_resolve_successful_payment_is_idempotent_when_already_success():
    tx = PaymentTransaction(
        id=uuid.uuid4(),
        internal_reference="ref-2",
        status=PaymentStatus.SUCCESS.value,
        plan_id=uuid.uuid4(),
        site_id=uuid.uuid4(),
        amount_ghs=Decimal("4.00"),
        payment_method=PaymentMethod.MTN_MOMO.value,
        provider="mtn",
        voucher_id=uuid.uuid4(),
    )
    db = FakeDb([tx])
    service = _service()

    updated = await service.resolve_successful_payment(
        db,
        internal_reference="ref-2",
        trigger_source="webhook",
    )

    assert updated.voucher_id == tx.voucher_id
    assert len([obj for obj in db.added if isinstance(obj, Voucher)]) == 0


def _otp_pending_tx() -> PaymentTransaction:
    return PaymentTransaction(
        id=uuid.uuid4(),
        internal_reference="ref-otp",
        status=PaymentStatus.PENDING.value,
        plan_id=uuid.uuid4(),
        site_id=uuid.uuid4(),
        amount_ghs=Decimal("2.00"),
        payment_method=PaymentMethod.MTN_MOMO.value,
        provider="paystack",
        provider_reference="charge-ref",
        next_action=PaymentNextAction.ENTER_OTP.value,
    )


class _VerifyMustNotRunProvider:
    async def verify(self, *args, **kwargs):
        raise RuntimeError("verify-called")


@pytest.mark.asyncio
async def test_refresh_skips_verify_while_awaiting_customer_input(monkeypatch):
    tx = _otp_pending_tx()
    service = _service()

    async def _fake_provider(db, t):
        return _VerifyMustNotRunProvider()

    monkeypatch.setattr(service, "provider_for_transaction", _fake_provider)

    out = await service.refresh_transaction_status(FakeDb([]), tx=tx, force=False)

    assert out is tx
    assert out.status == PaymentStatus.PENDING.value
    assert out.next_action == PaymentNextAction.ENTER_OTP.value


@pytest.mark.asyncio
async def test_refresh_still_verifies_awaiting_input_tx_when_forced(monkeypatch):
    tx = _otp_pending_tx()
    service = _service()

    async def _fake_provider(db, t):
        return _VerifyMustNotRunProvider()

    monkeypatch.setattr(service, "provider_for_transaction", _fake_provider)

    with pytest.raises(RuntimeError, match="verify-called"):
        await service.refresh_transaction_status(FakeDb([]), tx=tx, force=True)
