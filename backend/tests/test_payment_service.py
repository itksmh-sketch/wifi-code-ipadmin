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
from src.modules.resellers.wallet_service import _pick_commission_rule, _RuleView


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
    dummy = DummyProvider()
    return PaymentService(dummy, dummy, dummy, dummy)


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


def _rule(reseller_id, plan_id, value: str, is_active: bool = True) -> _RuleView:
    return _RuleView(
        id=uuid.uuid4(),
        reseller_id=reseller_id,
        plan_id=plan_id,
        type="percentage",
        value=Decimal(value),
        is_active=is_active,
    )


def test_commission_priority_level_1_reseller_and_plan_specific():
    reseller_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    rules = [
        _rule(None, None, "1.0000"),
        _rule(None, plan_id, "2.0000"),
        _rule(reseller_id, None, "3.0000"),
        _rule(reseller_id, plan_id, "4.0000"),
    ]
    picked = _pick_commission_rule(rules, reseller_id=reseller_id, plan_id=plan_id)
    assert picked is not None
    assert picked.reseller_id == reseller_id
    assert picked.plan_id == plan_id
    assert picked.value == Decimal("4.0000")


def test_commission_priority_level_2_reseller_specific_plan_null():
    reseller_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    rules = [
        _rule(None, None, "1.0000"),
        _rule(None, plan_id, "2.0000"),
        _rule(reseller_id, None, "3.0000"),
    ]
    picked = _pick_commission_rule(rules, reseller_id=reseller_id, plan_id=plan_id)
    assert picked is not None
    assert picked.reseller_id == reseller_id
    assert picked.plan_id is None
    assert picked.value == Decimal("3.0000")


def test_commission_priority_level_3_plan_specific_reseller_null():
    reseller_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    rules = [
        _rule(None, None, "1.0000"),
        _rule(None, plan_id, "2.0000"),
    ]
    picked = _pick_commission_rule(rules, reseller_id=reseller_id, plan_id=plan_id)
    assert picked is not None
    assert picked.reseller_id is None
    assert picked.plan_id == plan_id
    assert picked.value == Decimal("2.0000")


def test_commission_priority_level_4_global_default():
    reseller_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    rules = [
        _rule(None, None, "1.0000"),
    ]
    picked = _pick_commission_rule(rules, reseller_id=reseller_id, plan_id=plan_id)
    assert picked is not None
    assert picked.reseller_id is None
    assert picked.plan_id is None
    assert picked.value == Decimal("1.0000")


def test_commission_priority_level_5_no_rule_returns_none():
    reseller_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    picked = _pick_commission_rule([], reseller_id=reseller_id, plan_id=plan_id)
    assert picked is None
