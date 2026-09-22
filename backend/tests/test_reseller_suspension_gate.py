"""Unit tests for the operator-suspension gate on reseller voucher purchases.

Suspension is the platform's billing-enforcement lever, but it used to reach
only the captive portal (``/portal/initiate-payment``). ``POST
/reseller/vouchers/purchase`` had no check at all, so a suspended operator's
resellers kept drawing down wallet balance and kept minting vouchers — the
operator's business ran on untouched, which defeats the point of suspending
them.

Login stays deliberately open (matching admin login): a reseller can sign in
and see the account, they just cannot transact.

Pure unit tests: no server, no DB, no Redis. Defines neither ``BASE_URL`` nor
``_request``, so tests/conftest.py classifies this module as unit-only.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from src.db.models import ISPOperator, Plan, Reseller, Voucher
from src.modules.resellers import routes as reseller_routes


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
        self.committed = False
        self.rolled_back = False

    def in_transaction(self) -> bool:
        return False

    def begin(self):
        return _BeginCtx()

    async def execute(self, statement):
        self.executed.append(statement)
        return _ScalarResult(self.results.pop(0) if self.results else None)

    def add(self, obj):
        if isinstance(obj, Voucher) and not obj.id:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    async def flush(self):
        return None

    async def refresh(self, _):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    @property
    def vouchers_added(self):
        return [o for o in self.added if isinstance(o, Voucher)]


def _operator(status: str) -> ISPOperator:
    return ISPOperator(
        id=uuid.uuid4(),
        name="Osu Hotspot",
        slug="osu-hotspot",
        contact_email="ops@example.test",
        status=status,
    )


def _reseller(operator_id) -> Reseller:
    return Reseller(
        id=uuid.uuid4(),
        isp_operator_id=operator_id,
        email="reseller@example.test",
        role="reseller",
        site_id=None,
        town_id=None,
        is_active=True,
    )


def _plan(operator_id) -> Plan:
    return Plan(
        id=uuid.uuid4(),
        isp_operator_id=operator_id,
        name="1 hour",
        type="time",
        duration_minutes=60,
        data_limit_mb=None,
        download_speed_kbps=1024,
        upload_speed_kbps=512,
        price_ghs=Decimal("4.00"),
        site_id=None,
        is_active=True,
    )


class _Body:
    def __init__(self, plan_id, quantity=2):
        self.plan_id = plan_id
        self.quantity = quantity


@pytest.fixture
def funded_wallet(monkeypatch):
    """Wallet with plenty of balance; records every purchase() debit."""
    debits = []

    async def _commission(db, reseller_id, plan_id):
        return Decimal("1.00")

    async def _balance(db, reseller_id):
        return Decimal("500.00")

    async def _purchase(db, *, reseller_id, voucher_id, plan_id):
        debits.append(voucher_id)
        return None

    monkeypatch.setattr(reseller_routes.wallet_service, "calculate_commission", _commission)
    monkeypatch.setattr(reseller_routes.wallet_service, "get_balance", _balance)
    monkeypatch.setattr(reseller_routes.wallet_service, "purchase", _purchase)
    return debits


@pytest.mark.asyncio
async def test_suspended_operator_blocks_reseller_purchase(funded_wallet):
    operator = _operator("suspended")
    reseller = _reseller(operator.id)
    plan = _plan(operator.id)
    db = FakeDb([operator, plan])

    with pytest.raises(HTTPException) as exc:
        await reseller_routes.reseller_purchase_vouchers(_Body(plan.id), reseller, db)

    assert exc.value.status_code == 503
    # Same status and wording the captive portal already returns.
    assert exc.value.detail == "This hotspot is temporarily unavailable for new purchases"
    # Nothing minted, nothing debited, wallet untouched.
    assert db.vouchers_added == []
    assert funded_wallet == []
    assert db.committed is False
    # Bailed on the operator lookup, before the plan was even read.
    assert len(db.executed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["approved", "pending"])
async def test_non_suspended_operator_can_still_purchase(funded_wallet, status):
    operator = _operator(status)
    reseller = _reseller(operator.id)
    plan = _plan(operator.id)
    db = FakeDb([operator, plan])

    result = await reseller_routes.reseller_purchase_vouchers(_Body(plan.id, quantity=2), reseller, db)

    assert result["quantity"] == 2
    assert len(result["voucher_codes"]) == 2
    assert len(db.vouchers_added) == 2
    assert all(v.source == "reseller" for v in db.vouchers_added)
    assert len(funded_wallet) == 2
    # price 4.00 - commission 1.00 = 3.00 each
    assert result["unit_cost_ghs"] == 3.00
    assert result["total_cost_ghs"] == 6.00


@pytest.mark.asyncio
async def test_missing_operator_row_does_not_block_purchase(funded_wallet):
    """Mirrors the portal's `if operator and ...` — an absent row is not a
    suspension, and must not silently kill a reseller's ability to trade."""
    reseller = _reseller(uuid.uuid4())
    plan = _plan(reseller.isp_operator_id)
    db = FakeDb([None, plan])

    result = await reseller_routes.reseller_purchase_vouchers(_Body(plan.id, quantity=1), reseller, db)

    assert len(result["voucher_codes"]) == 1


@pytest.mark.asyncio
async def test_reseller_login_stays_open_while_suspended(monkeypatch):
    """Only the revenue-generating action is blocked — sign-in is not."""
    operator = _operator("suspended")
    reseller = _reseller(operator.id)
    db = FakeDb([reseller])

    async def _no_rate_limit(*args, **kwargs):
        return None

    async def _touch_login(reseller_id, db):
        return None

    monkeypatch.setattr(reseller_routes, "enforce_rate_limit", _no_rate_limit)
    monkeypatch.setattr(reseller_routes, "update_reseller_last_login", _touch_login)
    monkeypatch.setattr(reseller_routes, "verify_reseller_password", lambda raw, hashed: True)

    class _Login:
        email = "reseller@example.test"
        password = "pw"

    class _Req:
        client = type("C", (), {"host": "1.2.3.4"})()

    token = await reseller_routes.reseller_login(_Login(), _Req(), db)

    assert token.access_token
    assert token.refresh_token
    # No ISPOperator lookup happened at all during login.
    assert len(db.executed) == 1
