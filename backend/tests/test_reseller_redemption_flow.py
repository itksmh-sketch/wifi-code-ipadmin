"""Reseller redemptions (migration 060) against a REAL database, driven by
RADIUS's own Accounting-Start and authorize queries read from
freeradius/sql.conf:

  * a reseller's code earns exactly once, when a customer first comes online,
    whatever the retries, reconnects, second devices or concurrent starts;
  * the price, commission type, rate and amount are stored on the row, so a
    later rate change never alters it; a fixed amount is capped at the price;
  * reclaimed and expired codes can't be redeemed and earn nothing;
  * operator stock and portal sales never create rows; wallet-era allocations
    never earn;
  * the trigger fails open, and the nightly repair job adds the missing row,
    marked as repaired;
  * redemption rows can't be changed or deleted.

Needs the repo's freeradius/ directory (at /freeradius when the backend dir is
mounted alone) and a disposable database:

    RESELLER_REDEMPTION_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>

(tests/throwaway_db.py run sets both.)
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("RESELLER_REDEMPTION_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs RESELLER_REDEMPTION_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import select, text, update
    from sqlalchemy.exc import DBAPIError

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import ISPOperator, Plan, Reseller, ResellerVoucherAllocation, Voucher
    from src.jobs.reseller_redemption_repair import repair_reseller_redemptions
    from src.jobs.voucher_expiry import _expire_vouchers_locked
    from src.modules.resellers import consignment
    from src.portal.routes import DISABLED_DETAIL, EXPIRED_DETAIL
    from reseller_support import (
        accounting_start, audit_rows, fresh, make_admin, make_operator, make_plan, make_reseller, make_router, now,
        portal_login, radius_accepts, redemptions,
    )


@pytest_asyncio.fixture(loop_scope="module")
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def setup(*, price="10.00", commission=("percent", "12.50"), default=None, quantity=3, redeem_by=None):
    """An operator with a router, a plan, an admin and a reseller holding
    ``quantity`` allocated codes."""
    operator = await make_operator(**({"reseller_commission_type": default[0],
                                       "reseller_commission_value": Decimal(default[1])} if default else {}))
    router = await make_router(operator)
    plan = await make_plan(operator, price=price)
    admin, _ = await make_admin(operator)
    values = {"commission_type": commission[0], "commission_value": Decimal(commission[1])} if commission else {}
    reseller = await make_reseller(operator, **values)
    vouchers = await allocate(operator, reseller, plan, admin, quantity, redeem_by)
    return operator, router, plan, admin, reseller, vouchers


async def allocate(operator, reseller, plan, admin, quantity, redeem_by=None):
    async with async_session_factory() as db:
        op = await db.get(ISPOperator, operator.id)
        res = await db.get(Reseller, reseller.id)
        _, vouchers = await consignment.allocate(db, operator=op, reseller=res, plan_id=plan.id, quantity=quantity,
                                                 redeem_by=redeem_by, admin_id=admin.id)
        await db.commit()
        return [await db.get(Voucher, v.id) for v in vouchers]


async def set_commission(reseller, ctype, value):
    async with async_session_factory() as db:
        await db.execute(update(Reseller).where(Reseller.id == reseller.id)
                         .values(commission_type=ctype, commission_value=None if value is None else Decimal(value)))
        await db.commit()


def amounts(row):
    return (row.price_ghs, row.commission_type, row.commission_value, row.commission_ghs, row.owed_ghs, row.source)


# ── exactly once, with the rate fixed at redemption ───────────────────────


async def test_first_login_records_the_redemption_with_its_rate():
    _, router, plan, _, reseller, (code, *_rest) = await setup()
    assert code.expires_at is None, "the plan's clock must not start at allocation"
    await accounting_start(code, router)

    [row] = await redemptions(code.id)
    assert amounts(row) == (Decimal("10.00"), "percent", Decimal("12.50"), Decimal("1.25"), Decimal("8.75"), "trigger")
    assert row.reseller_id == reseller.id and row.plan_id == plan.id
    voucher = await fresh(Voucher, code.id)
    assert voucher.status == "active" and row.redeemed_at == voucher.activated_at
    # The plan's clock started at first use.
    assert timedelta(minutes=59) < voucher.expires_at - voucher.activated_at <= timedelta(minutes=60)


async def test_retries_reconnects_and_second_devices_never_earn_again():
    _, router, _, _, _, (code, *_rest) = await setup()
    session = await accounting_start(code, router)
    await accounting_start(code, router, session)  # the router retrying the same Accounting-Start
    await accounting_start(code, router)  # a reconnect: a new session, same code
    await accounting_start(code, router)  # a second device
    assert len(await redemptions(code.id)) == 1


async def test_concurrent_first_logins_earn_once():
    _, router, _, _, _, (code, *_rest) = await setup()
    await asyncio.gather(*(accounting_start(code, router) for _ in range(5)))
    assert len(await redemptions(code.id)) == 1


async def test_a_code_cant_earn_twice_even_after_being_put_back_to_unused():
    """Negative: disabled and re-enabled to unused, then activated again: the
    trigger fires again and UNIQUE(voucher_id) keeps the one row."""
    _, router, _, _, _, (code, *_rest) = await setup()
    await accounting_start(code, router)
    async with async_session_factory() as db:
        for status in ("disabled", "unused", "active"):
            await db.execute(update(Voucher).where(Voucher.id == code.id).values(status=status))
        await db.commit()
    await accounting_start(code, router)
    assert len(await redemptions(code.id)) == 1


async def test_a_later_rate_change_leaves_past_rows_alone():
    _, router, _, _, reseller, (first, second, third) = await setup()
    await accounting_start(first, router)
    await set_commission(reseller, "fixed", "3.00")
    await accounting_start(second, router)
    await set_commission(reseller, "fixed", "50.00")  # more than the price: capped
    await accounting_start(third, router)

    rows = {r.voucher_id: r for r in await redemptions(reseller_id=reseller.id)}
    assert amounts(rows[first.id])[:5] == (Decimal("10.00"), "percent", Decimal("12.50"), Decimal("1.25"), Decimal("8.75"))
    assert amounts(rows[second.id])[:5] == (Decimal("10.00"), "fixed", Decimal("3.00"), Decimal("3.00"), Decimal("7.00"))
    assert amounts(rows[third.id])[:5] == (Decimal("10.00"), "fixed", Decimal("50.00"), Decimal("10.00"), Decimal("0.00"))


async def test_the_operator_default_applies_to_a_reseller_without_its_own():
    _, router, _, _, reseller, (code, *_rest) = await setup(commission=None, default=("percent", "5"))
    await accounting_start(code, router)
    [row] = await redemptions(code.id)
    assert (row.commission_type, row.commission_value, row.commission_ghs) == ("percent", Decimal("5.00"), Decimal("0.50"))


async def test_percent_rounds_half_up_to_the_pesewa():
    _, router, _, _, _, (code, *_rest) = await setup(price="2.50", commission=("percent", "7"))
    await accounting_start(code, router)
    [row] = await redemptions(code.id)
    assert (row.commission_ghs, row.owed_ghs) == (Decimal("0.18"), Decimal("2.32"))  # 0.175 -> 0.18


async def test_the_price_owed_is_the_one_at_allocation():
    _, router, plan, _, _, (code, *_rest) = await setup()
    async with async_session_factory() as db:
        await db.execute(update(Plan).where(Plan.id == plan.id).values(price_ghs=Decimal("15.00")))
        await db.commit()
    await accounting_start(code, router)
    [row] = await redemptions(code.id)
    assert (row.price_ghs, row.owed_ghs) == (Decimal("10.00"), Decimal("8.75"))


# ── what can't be redeemed or earn ────────────────────────────────────────


async def test_a_reclaimed_code_cant_be_redeemed_or_earn(client):
    operator, router, _, admin, reseller, (code, *_rest) = await setup()
    assert await radius_accepts(code, router)
    async with async_session_factory() as db:
        assert await consignment.reclaim(db, operator_id=operator.id, reseller=await db.get(Reseller, reseller.id),
                                         voucher_ids=[code.id], allocation_id=None, reason="Agent left",
                                         admin_id=admin.id) == 1
        await db.commit()

    res = await portal_login(client, code.code, router)
    assert (res.status_code, res.json()["detail"]) == (400, DISABLED_DETAIL)
    assert not await radius_accepts(code, router)
    await accounting_start(code, router)  # even if a router sent one anyway
    assert (await fresh(Voucher, code.id)).status == "disabled"
    assert await redemptions(code.id) == []


async def test_a_reclaimed_allocation_never_earns_even_if_the_voucher_is_forced_active():
    """Negative: the trigger only credits a code still allocated."""
    operator, _, _, admin, reseller, (code, *_rest) = await setup()
    async with async_session_factory() as db:
        await consignment.reclaim(db, operator_id=operator.id, reseller=await db.get(Reseller, reseller.id),
                                  voucher_ids=[code.id], allocation_id=None, reason="x", admin_id=admin.id)
        await db.commit()
    async with async_session_factory() as db:
        await db.execute(update(Voucher).where(Voucher.id == code.id).values(status="unused"))
        await db.execute(update(Voucher).where(Voucher.id == code.id).values(status="active", activated_at=now()))
        await db.commit()
    assert await redemptions(code.id) == []


async def test_an_expired_code_cant_be_redeemed_or_earn(client):
    _, router, _, _, _, (code, other, _third) = await setup()
    async with async_session_factory() as db:
        await db.execute(update(Voucher).where(Voucher.id.in_([code.id, other.id]))
                         .values(redeem_by=now() - timedelta(minutes=1)))
        await db.commit()

    # The portal refuses at once, before the job has run.
    res = await portal_login(client, code.code, router)
    assert (res.status_code, res.json()["detail"]) == (400, EXPIRED_DETAIL)
    await _expire_vouchers_locked()
    for v in (code, other):
        assert (await fresh(Voucher, v.id)).status == "expired"
        [row] = await audit_rows("voucher.expire", v.id)
        assert row.detail["via"] == "sell_by" and row.changes == {"status": {"before": "unused", "after": "expired"}}
    assert not await radius_accepts(code, router)
    await accounting_start(code, router)
    assert (await fresh(Voucher, code.id)).status == "expired"
    assert await redemptions(code.id) == []


async def test_a_code_before_its_sell_by_still_works(client):
    _, router, _, _, _, (code, *_rest) = await setup(redeem_by=(now() + timedelta(days=2)).isoformat())
    await _expire_vouchers_locked()
    assert (await fresh(Voucher, code.id)).status == "unused"
    assert (await portal_login(client, code.code, router)).status_code == 200


async def test_operator_stock_and_wallet_era_codes_never_earn():
    operator, router, plan, _, reseller, _ = await setup(quantity=1)
    async with async_session_factory() as db:
        manual = Voucher(isp_operator_id=operator.id, plan_id=plan.id, code=f"M-{uuid.uuid4().hex[:12]}",
                         username=f"MU-{uuid.uuid4().hex[:12]}", password="pw", status="unused", source="manual")
        legacy = Voucher(isp_operator_id=operator.id, plan_id=plan.id, code=f"L-{uuid.uuid4().hex[:12]}",
                         username=f"LU-{uuid.uuid4().hex[:12]}", password="pw", status="unused", source="reseller")
        db.add_all([manual, legacy])
        await db.flush()
        # A wallet-era allocation: no allocation_id, already paid for.
        db.add(ResellerVoucherAllocation(reseller_id=reseller.id, voucher_id=legacy.id, purchase_price_ghs=Decimal("9"),
                                         isp_operator_id=operator.id))
        await db.commit()
    for v in (manual, legacy):
        await accounting_start(v, router)
        assert (await fresh(Voucher, v.id)).status == "active"
        assert await redemptions(v.id) == []


async def test_an_allocation_row_of_another_operator_never_earns():
    """Negative: a voucher of operator A whose allocation row claims operator
    B (inconsistent data) is not credited to anyone."""
    operator, router, _, _, _, (code, *_rest) = await setup(quantity=1)
    other = await make_operator()
    async with async_session_factory() as db:
        await db.execute(update(ResellerVoucherAllocation).where(ResellerVoucherAllocation.voucher_id == code.id)
                         .values(isp_operator_id=other.id))
        await db.commit()
    await accounting_start(code, router)
    assert (await fresh(Voucher, code.id)).status == "active"
    assert await redemptions(code.id) == []


# ── fail-open and repair ──────────────────────────────────────────────────


async def test_trigger_fails_open_and_the_repair_job_adds_the_row():
    _, router, _, _, reseller, (code, untouched, _third) = await setup()
    async with async_session_factory() as db:
        await db.execute(text("ALTER FUNCTION reseller_redemption_insert(uuid, timestamptz, text) RENAME TO rr_insert_off"))
        await db.commit()
    try:
        await accounting_start(code, router)
    finally:
        async with async_session_factory() as db:
            await db.execute(text("ALTER FUNCTION rr_insert_off(uuid, timestamptz, text) RENAME TO reseller_redemption_insert"))
            await db.commit()
    voucher = await fresh(Voucher, code.id)
    assert voucher.status == "active", "the customer's session must be metered even though the hook failed"
    assert await redemptions(code.id) == []

    stats = await repair_reseller_redemptions()
    assert stats["repaired"] >= 1 and stats["errors"] == 0
    [row] = await redemptions(code.id)
    assert row.source == "repair" and row.redeemed_at == voucher.activated_at
    assert (row.commission_ghs, row.owed_ghs) == (Decimal("1.25"), Decimal("8.75"))
    [audited] = await audit_rows("reseller.redemption_repaired", code.id)
    assert audited.actor_type == "system" and audited.detail["reseller_id"] == str(reseller.id)
    assert code.code not in str(audited.detail) and code.code not in (audited.summary or "")

    # Idempotent, and an unredeemed code is left alone.
    assert (await repair_reseller_redemptions())["repaired"] == 0
    assert len(await redemptions(code.id)) == 1 and await redemptions(untouched.id) == []


# ── append-only ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("statement", [
    "UPDATE reseller_redemptions SET commission_ghs = 0 WHERE voucher_id = :v",
    "DELETE FROM reseller_redemptions WHERE voucher_id = :v",
    "TRUNCATE reseller_redemptions",
])
async def test_redemptions_cant_be_changed_or_deleted(statement):
    _, router, _, _, _, (code, *_rest) = await setup(quantity=1)
    await accounting_start(code, router)
    async with async_session_factory() as db:
        with pytest.raises(DBAPIError, match="append-only"):
            await db.execute(text(statement), {"v": code.id} if ":v" in statement else {})
        await db.rollback()
    [row] = await redemptions(code.id)
    assert row.commission_ghs == Decimal("1.25")
