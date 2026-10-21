"""The reseller running account (migration 060), through the API, against a
REAL database:

  * outstanding = value of redeemed codes - commission - payments + reversals,
    correct after partial payments, an overpayment (shown as credit, never a
    negative outstanding) and a reversal; the ledger's running balance agrees;
  * a payment is reversed in full, at most once, and a reversal never is,
    including two reversals racing; entries can't be edited or deleted, and
    the database refuses a reversal that doesn't match its payment;
  * recording and reversing need money and a recent PIN, and are audited;
  * a reseller sees its own account read-only and can't record anything;
    operator A can't see or touch operator B's resellers' accounts.

Needs the repo's freeradius/ directory (at /freeradius when the backend dir is
mounted alone) and a disposable database:

    RESELLER_ACCOUNT_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>

(tests/throwaway_db.py run sets both.)
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("RESELLER_ACCOUNT_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs RESELLER_ACCOUNT_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import select, text, update
    from sqlalchemy.exc import DBAPIError

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import AdminUser, Reseller, ResellerAccountEntry
    from reseller_support import (
        accounting_start, audit_rows, make_admin, make_operator, make_plan, make_reseller, make_router,
        reseller_headers, voucher_by_code,
    )

API = "http://testserver/api/v1"


@pytest_asyncio.fixture(loop_scope="module")
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=API) as c:
        yield c


async def tenant(client, *, redeemed=0, price="10.00", commission=("percent", "10")):
    """An operator whose reseller has ``redeemed`` codes redeemed (each owes
    price minus commission)."""
    operator = await make_operator()
    router = await make_router(operator)
    plan = await make_plan(operator, price=price)
    admin, headers = await make_admin(operator)
    reseller = await make_reseller(operator, commission_type=commission[0], commission_value=Decimal(commission[1]))
    t = {"operator": operator, "router": router, "plan": plan, "admin": admin, "h": headers, "reseller": reseller}
    if redeemed:
        res = await client.post(f"/admin/resellers/{reseller.id}/allocations", headers=headers,
                                json={"plan_id": str(plan.id), "quantity": redeemed})
        assert res.status_code == 200, res.text
        for code in res.json()["codes"]:
            await accounting_start(await voucher_by_code(code), router)
    return t


async def pay(client, t, amount, **body):
    return await client.post(f"/admin/resellers/{t['reseller'].id}/payments", headers=t["h"],
                             json={"amount_ghs": amount, **body})


async def reverse(client, t, entry_id, reason="Counted twice", headers=None):
    return await client.post(f"/admin/resellers/{t['reseller'].id}/payments/{entry_id}/reversal",
                             headers=headers or t["h"], json={"reason": reason})


async def totals(client, t):
    return (await client.get(f"/admin/resellers/{t['reseller'].id}/account", headers=t["h"])).json()["totals"]


def money(totals):
    return (totals["owed_ghs"], totals["paid_ghs"], totals["outstanding_ghs"], totals["credit_ghs"])


# ── the balance ───────────────────────────────────────────────────────────


async def test_balance_after_partial_payments_an_overpayment_and_a_reversal(client):
    t = await tenant(client, redeemed=3)  # 3 x (10.00 - 1.00) = 27.00 owed
    assert money(await totals(client, t)) == (27.0, 0.0, 27.0, 0.0)

    first = await pay(client, t, "10.00", received_on=(date.today() - timedelta(days=1)).isoformat(), note="Monday")
    assert first.status_code == 200 and (first.json()["outstanding_ghs"], first.json()["paid_ghs"]) == (17.0, 10.0)
    assert money(await totals(client, t)) == (27.0, 10.0, 17.0, 0.0)
    await pay(client, t, 7)
    assert money(await totals(client, t)) == (27.0, 17.0, 10.0, 0.0)
    over = (await pay(client, t, "15.50")).json()
    assert money(await totals(client, t)) == (27.0, 32.5, 0.0, 5.5), "an overpayment is credit, not a negative balance"
    rev = await reverse(client, t, over["id"], "Entered against the wrong reseller")
    assert rev.status_code == 200
    assert money(await totals(client, t)) == (27.0, 17.0, 10.0, 0.0)

    ledger = (await client.get(f"/admin/resellers/{t['reseller'].id}/account", headers=t["h"])).json()
    assert ledger["balance_ghs"] == 10.0
    # In the order they were recorded (the first payment's received_on is
    # yesterday, but it was entered after the redemptions).
    assert [l["type"] for l in ledger["lines"]] == ["redemptions", "payment", "payment", "payment", "reversal"]
    assert [l["balance_ghs"] for l in ledger["lines"]] == [27.0, 17.0, 10.0, -5.5, 10.0]
    redemptions = ledger["lines"][0]
    assert (redemptions["count"], redemptions["value_ghs"], redemptions["commission_ghs"], redemptions["repaired"]) == (3, 30.0, 3.0, 0)
    assert [l.get("reversed") for l in ledger["lines"] if l["type"] == "payment"] == [False, False, True]


async def test_the_reseller_sees_the_same_numbers_read_only(client):
    t = await tenant(client, redeemed=2)
    await pay(client, t, 5)
    rh = await reseller_headers(client, t["reseller"])
    mine = (await client.get("/reseller/account", headers=rh)).json()
    assert money(mine["totals"]) == money(await totals(client, t)) == (18.0, 5.0, 13.0, 0.0)
    assert all("recorded_by_admin_id" not in line for line in mine["lines"]), "who recorded it is the operator's business"
    # No reseller route records or reverses anything.
    for path in ("/reseller/payments", "/reseller/account", f"/reseller/account/{uuid.uuid4()}/reversal"):
        assert (await client.post(path, headers=rh, json={"amount_ghs": 5})).status_code in (404, 405)
    assert (await pay(client, {**t, "h": rh}, 5)).status_code == 401
    assert money((await client.get("/reseller/account", headers=rh)).json()["totals"]) == (18.0, 5.0, 13.0, 0.0)


# ── reversals ─────────────────────────────────────────────────────────────


async def test_a_reversal_cant_be_reversed_and_a_payment_only_once(client):
    t = await tenant(client, redeemed=1)
    payment = (await pay(client, t, 9)).json()["id"]
    reversal = (await reverse(client, t, payment)).json()["id"]
    again = await reverse(client, t, payment)
    assert again.status_code == 409 and "already" in again.json()["detail"]
    of_reversal = await reverse(client, t, reversal)
    assert of_reversal.status_code == 409 and "can't be reversed" in of_reversal.json()["detail"]
    assert money(await totals(client, t)) == (9.0, 0.0, 9.0, 0.0)


async def test_two_reversals_racing_leave_one(client):
    t = await tenant(client, redeemed=1)
    payment = (await pay(client, t, 9)).json()["id"]
    results = await asyncio.gather(*(reverse(client, t, payment) for _ in range(4)))
    assert sorted(r.status_code for r in results) == [200, 409, 409, 409]
    async with async_session_factory() as db:
        rows = (await db.execute(select(ResellerAccountEntry).where(ResellerAccountEntry.reverses_entry_id == uuid.UUID(payment)))).scalars().all()
    assert len(rows) == 1


async def test_reversal_needs_a_reason_and_a_real_payment(client):
    t = await tenant(client)
    payment = (await pay(client, t, 4)).json()["id"]
    assert (await reverse(client, t, payment, "   ")).status_code == 400
    assert (await reverse(client, t, uuid.uuid4())).status_code == 404


@pytest.mark.parametrize("bad", [
    "kind 'payment' with a reverses_entry_id", "amount differs", "another reseller", "of a reversal",
])
async def test_the_database_refuses_a_reversal_that_doesnt_match(client, bad):
    t = await tenant(client)
    other = await make_reseller(t["operator"])
    payment = uuid.UUID((await pay(client, t, 4)).json()["id"])
    reversal = uuid.UUID((await reverse(client, t, payment)).json()["id"])
    second = uuid.UUID((await pay(client, t, 6)).json()["id"])
    values = {"op": t["operator"].id, "res": t["reseller"].id, "kind": "reversal", "amount": Decimal("6"),
              "target": second, "admin": t["admin"].id}
    if bad == "kind 'payment' with a reverses_entry_id":
        values["kind"] = "payment"
    elif bad == "amount differs":
        values["amount"] = Decimal("5")
    elif bad == "another reseller":
        values["res"] = other.id
    else:
        values.update(target=reversal, amount=Decimal("4"))
    async with async_session_factory() as db:
        with pytest.raises(DBAPIError):
            await db.execute(text(
                "INSERT INTO reseller_account_entries (isp_operator_id, reseller_id, kind, amount_ghs, reverses_entry_id, "
                "received_on, recorded_by_admin_id) VALUES (:op, :res, :kind, :amount, :target, current_date, :admin)"
            ), values)
        await db.rollback()


@pytest.mark.parametrize("statement", [
    "UPDATE reseller_account_entries SET amount_ghs = 1 WHERE reseller_id = :r",
    "DELETE FROM reseller_account_entries WHERE reseller_id = :r",
    "TRUNCATE reseller_account_entries",
])
async def test_entries_cant_be_edited_or_deleted(client, statement):
    t = await tenant(client)
    await pay(client, t, 3)
    async with async_session_factory() as db:
        with pytest.raises(DBAPIError, match="append-only"):
            await db.execute(text(statement), {"r": t["reseller"].id} if ":r" in statement else {})
        await db.rollback()
    assert (await totals(client, t))["paid_ghs"] == 3.0


# ── validation, permissions, audit ────────────────────────────────────────


@pytest.mark.parametrize("amount", [0, -5, "-0.01", "NaN", "Infinity", "1.005", "100000.01", "ten", None])
async def test_bad_amounts_are_refused(client, amount):
    t = await tenant(client)
    assert (await pay(client, t, amount)).status_code == 400
    assert (await pay(client, t, 1, received_on=(date.today() + timedelta(days=1)).isoformat())).status_code == 400
    assert (await totals(client, t))["paid_ghs"] == 0.0


async def test_payments_and_reversals_need_a_recent_pin(client):
    t = await tenant(client)
    payment = (await pay(client, t, 4)).json()["id"]
    async with async_session_factory() as db:
        await db.execute(update(AdminUser).where(AdminUser.id == t["admin"].id).values(pin_verified_until=None))
        await db.commit()
    res = await pay(client, t, 4)
    assert res.status_code == 403 and res.headers.get("X-Pin-Required") == "verify"
    res = await reverse(client, t, payment)
    assert res.status_code == 403 and res.headers.get("X-Pin-Required") == "verify"
    assert (await totals(client, t))["paid_ghs"] == 4.0


async def test_staff_cant_record_or_see_payments(client):
    t = await tenant(client)
    _, staff = await make_admin(t["operator"], role="staff")
    rid = t["reseller"].id
    assert (await client.post(f"/admin/resellers/{rid}/payments", headers=staff, json={"amount_ghs": 4})).status_code == 403
    assert (await client.get(f"/admin/resellers/{rid}/account", headers=staff)).status_code == 403


async def test_every_payment_and_reversal_is_audited(client):
    t = await tenant(client)
    payment = (await pay(client, t, "12.40", note="Cash at the kiosk")).json()["id"]
    await reverse(client, t, payment, "Wrong amount")
    recorded = await audit_rows("reseller.payment_recorded", t["reseller"].id)
    reversed_ = await audit_rows("reseller.payment_reversed", t["reseller"].id)
    assert [(r.detail["entry_id"], r.detail["amount_ghs"], r.detail["note"]) for r in recorded] == [(payment, "12.40", "Cash at the kiosk")]
    assert [(r.detail["reverses_entry_id"], r.detail["amount_ghs"], r.detail["reason"]) for r in reversed_] == [(payment, "12.40", "Wrong amount")]
    for row in recorded + reversed_:
        assert (row.actor_type, row.actor_id, row.isp_operator_id) == ("operator_user", t["admin"].id, t["operator"].id)


# ── isolation ─────────────────────────────────────────────────────────────


async def test_operator_a_cant_see_record_or_reverse_for_operator_bs_reseller(client):
    a, b = await tenant(client), await tenant(client, redeemed=1)
    b_payment = (await pay(client, b, 9)).json()["id"]
    b_rid = b["reseller"].id
    assert (await client.get(f"/admin/resellers/{b_rid}/account", headers=a["h"])).status_code == 404
    assert (await client.post(f"/admin/resellers/{b_rid}/payments", headers=a["h"], json={"amount_ghs": 1})).status_code == 404
    assert (await reverse(client, b, b_payment, headers=a["h"])).status_code == 404
    # B's entry named under A's own reseller.
    assert (await reverse(client, a, b_payment)).status_code == 404
    assert money(await totals(client, b)) == (9.0, 9.0, 0.0, 0.0)


async def test_a_reseller_never_sees_another_resellers_payments_or_earnings(client):
    t = await tenant(client, redeemed=2)
    await pay(client, t, 5, note="Kofi's cash")
    other = await make_reseller(t["operator"])
    rh = await reseller_headers(client, other)
    mine = (await client.get("/reseller/account", headers=rh)).json()
    assert mine["lines"] == [] and money(mine["totals"]) == (0.0, 0.0, 0.0, 0.0)
    assert (await client.get("/reseller/summary", headers=rh)).json()["totals"]["redeemed"] == 0
    # A reseller id passed as a parameter is ignored.
    again = (await client.get(f"/reseller/account?reseller_id={t['reseller'].id}", headers=rh)).json()
    assert again["lines"] == []


async def test_a_disabled_reseller_cant_read_its_account(client):
    t = await tenant(client, redeemed=1)
    rh = await reseller_headers(client, t["reseller"])
    async with async_session_factory() as db:
        await db.execute(update(Reseller).where(Reseller.id == t["reseller"].id).values(is_active=False))
        await db.commit()
    assert (await client.get("/reseller/account", headers=rh)).status_code == 401
