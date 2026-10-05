"""Unit tests for the worker-side voucher SMS: deliver_voucher_sms (the send
logic moved out of PaymentService) and the send_voucher_sms job / enqueue
helper. No network, no Redis, no database."""
import uuid
from decimal import Decimal

import pytest

from src.db.models import PaymentTransaction, Plan, Voucher
from src.jobs import voucher_sms
from src.jobs.worker import WorkerSettings
from src.modules.payments.types import PaymentStatus
from src.modules.sms import voucher_delivery
from src.modules.sms.types import SMSSendResult

OP = uuid.uuid4()


def _plan():
    return Plan(
        id=uuid.uuid4(), name="1 hour", type="time", duration_minutes=60, data_limit_mb=None,
        download_speed_kbps=1024, upload_speed_kbps=512, price_ghs=Decimal("4.00"), isp_operator_id=OP,
    )


class _Sender:
    def __init__(self, result=None, exc=None):
        self.sent = []
        self.result = result or SMSSendResult(success=True, provider_reference="m1")
        self.exc = exc

    async def send(self, *, to, message):
        self.sent.append((to, message))
        if self.exc:
            raise self.exc
        return self.result


class _Db:
    """Just enough AsyncSession for deliver_voucher_sms / send_voucher_sms."""

    def __init__(self, objects=None, plan=None):
        self.objects = objects or {}
        self.plan = plan

    async def get(self, model, key):
        return self.objects.get((model, key))

    async def execute(self, _stmt):
        plan = self.plan

        class _R:
            def scalar_one_or_none(self):
                return plan

        return _R()


@pytest.fixture
def sender(monkeypatch):
    s = _Sender()
    monkeypatch.setattr(voucher_delivery, "build_sms_provider", lambda k, c: s)
    return s


def _provider(monkeypatch, key):
    async def _resolve(_db, _op):
        return None if key is None else (key, {"api_key": "k", "sender_id": "SID"})

    monkeypatch.setattr(voucher_delivery, "resolve_active_sms_provider", _resolve)


# ── deliver_voucher_sms ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deliver_sends_through_operators_provider(monkeypatch, sender):
    _provider(monkeypatch, "hubtel")
    out = await voucher_delivery.deliver_voucher_sms(_Db(), operator_id=OP, to="233551234987", code="ABCD", plan=_plan())
    assert out == "sent"
    assert sender.sent[0][0] == "233551234987"
    assert "ABCD" in sender.sent[0][1]


@pytest.mark.asyncio
async def test_deliver_skips_without_provider(monkeypatch, sender):
    _provider(monkeypatch, None)
    out = await voucher_delivery.deliver_voucher_sms(_Db(), operator_id=OP, to="233551234987", code="ABCD", plan=_plan())
    assert out == "skipped_no_provider" and sender.sent == []


@pytest.mark.asyncio
async def test_deliver_platform_gateway_without_rate_does_not_send(monkeypatch, sender):
    _provider(monkeypatch, "arkesel_platform")

    async def _no_rate(_db):
        raise voucher_delivery.PlatformSMSRateNotConfigured()

    monkeypatch.setattr(voucher_delivery, "get_current_platform_sms_rate", _no_rate)
    out = await voucher_delivery.deliver_voucher_sms(_Db(), operator_id=OP, to="233551234987", code="ABCD", plan=_plan())
    assert out == "skipped_no_rate" and sender.sent == []


@pytest.mark.asyncio
async def test_deliver_platform_gateway_meters_after_accepted_send(monkeypatch, sender):
    _provider(monkeypatch, "arkesel_platform")
    metered = []

    async def _rate(_db):
        return Decimal("0.30")

    async def _meter(**kw):
        metered.append(kw)
        return True

    monkeypatch.setattr(voucher_delivery, "get_current_platform_sms_rate", _rate)
    monkeypatch.setattr(voucher_delivery, "record_platform_sms_usage", _meter)
    out = await voucher_delivery.deliver_voucher_sms(_Db(), operator_id=OP, to="233551234987", code="ABCD", plan=_plan())
    assert out == "sent"
    assert metered and metered[0]["provider_reference"] == "m1" and metered[0]["isp_operator_id"] == OP


@pytest.mark.asyncio
async def test_deliver_rejected_send_is_not_metered(monkeypatch):
    _provider(monkeypatch, "arkesel_platform")
    s = _Sender(result=SMSSendResult(success=False, error="arkesel_status_error: bad sender"))
    monkeypatch.setattr(voucher_delivery, "build_sms_provider", lambda k, c: s)

    async def _rate(_db):
        return Decimal("0.30")

    monkeypatch.setattr(voucher_delivery, "get_current_platform_sms_rate", _rate)
    monkeypatch.setattr(voucher_delivery, "record_platform_sms_usage", lambda **kw: pytest.fail("metered a failed send"))
    out = await voucher_delivery.deliver_voucher_sms(_Db(), operator_id=OP, to="233551234987", code="ABCD", plan=_plan())
    assert out == "failed"


@pytest.mark.asyncio
async def test_deliver_never_raises(monkeypatch):
    _provider(monkeypatch, "hubtel")
    s = _Sender(exc=RuntimeError("boom"))
    monkeypatch.setattr(voucher_delivery, "build_sms_provider", lambda k, c: s)
    out = await voucher_delivery.deliver_voucher_sms(_Db(), operator_id=OP, to="233551234987", code="ABCD", plan=_plan())
    assert out == "exception"


# ── send_voucher_sms job ─────────────────────────────────────────────────────

def _paid(status=PaymentStatus.SUCCESS.value, phone="233551234987", with_voucher=True):
    plan = _plan()
    voucher = Voucher(id=uuid.uuid4(), code="WXYZ-1234", isp_operator_id=OP)
    tx = PaymentTransaction(
        id=uuid.uuid4(), isp_operator_id=OP, plan_id=plan.id, status=status, phone_number=phone,
        voucher_id=voucher.id if with_voucher else None,
    )
    objects = {(PaymentTransaction, tx.id): tx, (Voucher, voucher.id): voucher}
    return tx, _Db(objects, plan)


@pytest.fixture
def job_db(monkeypatch):
    holder = {}

    class _Factory:
        async def __aenter__(self):
            return holder["db"]

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(voucher_sms, "async_session_factory", lambda: _Factory())
    return holder


@pytest.mark.asyncio
async def test_job_delivers_for_a_paid_transaction(monkeypatch, job_db):
    tx, db = _paid()
    job_db["db"] = db
    calls = []

    async def _deliver(_db, **kw):
        calls.append(kw)
        return "sent"

    monkeypatch.setattr(voucher_sms, "deliver_voucher_sms", _deliver)
    out = await voucher_sms.send_voucher_sms({"job_try": 1}, str(tx.id))
    assert out == "sent"
    assert calls == [{"operator_id": OP, "to": "233551234987", "code": "WXYZ-1234", "plan": db.plan}]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"status": PaymentStatus.PENDING.value}, {"phone": None}, {"with_voucher": False},
])
async def test_job_skips_ineligible_transactions(monkeypatch, job_db, kwargs):
    tx, db = _paid(**kwargs)
    job_db["db"] = db
    monkeypatch.setattr(voucher_sms, "deliver_voucher_sms", lambda *a, **k: pytest.fail("must not send"))
    assert await voucher_sms.send_voucher_sms({}, str(tx.id)) == "skipped_not_eligible"


def test_job_registered_with_a_single_try():
    fn = next(f for f in WorkerSettings.functions if getattr(f, "name", None) == voucher_sms.JOB_NAME)
    assert fn.max_tries == 1


@pytest.mark.asyncio
@pytest.mark.real_voucher_sms_enqueue
async def test_enqueue_uses_a_per_payment_job_id_and_closes_the_pool(monkeypatch):
    seen = {}

    class _Pool:
        async def enqueue_job(self, name, *args, _job_id=None):
            seen.update(name=name, args=args, job_id=_job_id)
            return object()

        async def aclose(self):
            seen["closed"] = True

    async def _pool():
        return _Pool()

    monkeypatch.setattr(voucher_sms, "get_redis_pool", _pool)
    tx_id = uuid.uuid4()
    assert await voucher_sms.enqueue_voucher_sms(tx_id) is True
    assert seen == {"name": "send_voucher_sms", "args": (str(tx_id),), "job_id": f"voucher_sms:{tx_id}", "closed": True}
