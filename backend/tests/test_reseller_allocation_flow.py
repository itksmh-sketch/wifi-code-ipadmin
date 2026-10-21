"""Reseller allocations, codes and reclaim (migration 060), through the API,
against a REAL database:

  * an operator allocates codes of one plan to its own reseller, priced at
    the plan's price then, with an optional sell-by; the plan's own clock
    still starts at first use;
  * counts (allocated, remaining, redeemed, expired, reclaimed, disabled) and
    the value at that price, the same for the operator and the reseller;
  * reclaim takes back unredeemed codes, all-or-nothing when named, and they
    stop working at the portal and RADIUS at once, for good;
  * operator A can't see, allocate to, or reclaim from operator B's
    resellers, nor use B's plans; a reseller sees only its own codes;
  * disabling a reseller ends its sessions but doesn't reclaim its codes;
  * suspension blocks allocation; staff can't reach any of it.

Needs the repo's freeradius/ directory (at /freeradius when the backend dir is
mounted alone) and a disposable database:

    RESELLER_ALLOCATION_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>

(tests/throwaway_db.py run sets both.)
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("RESELLER_ALLOCATION_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs RESELLER_ALLOCATION_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import update

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import ISPOperator, Plan, ResellerAllocation, Voucher
    from src.portal.routes import DISABLED_DETAIL
    from reseller_support import (
        accounting_start, audit_rows, fresh, make_admin, make_operator, make_plan, make_reseller, make_router, now,
        portal_login, radius_accepts, reseller_headers, voucher_by_code,
    )

API = "http://testserver/api/v1"


@pytest_asyncio.fixture(loop_scope="module")
async def client(monkeypatch_module):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=API) as c:
        yield c


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()

    async def no_limit(*args, **kwargs):
        return None

    for name, module in list(sys.modules.items()):
        if name.startswith("src.") and hasattr(module, "enforce_rate_limit"):
            mp.setattr(module, "enforce_rate_limit", no_limit)
    yield mp
    mp.undo()


async def tenant(**operator_values):
    operator = await make_operator(**operator_values)
    router = await make_router(operator)
    plan = await make_plan(operator, price="10.00")
    admin, headers = await make_admin(operator)
    reseller = await make_reseller(operator)
    return {"operator": operator, "router": router, "plan": plan, "admin": admin, "h": headers, "reseller": reseller}


async def allocate(client, t, quantity=3, **body):
    res = await client.post(f"/admin/resellers/{t['reseller'].id}/allocations", headers=t["h"],
                            json={"plan_id": str(t["plan"].id), "quantity": quantity, **body})
    assert res.status_code == 200, res.text
    return res.json()


def counts(totals):
    return {k: totals[k] for k in ("allocated", "remaining", "redeemed", "expired", "reclaimed", "disabled")}


# ── allocating ────────────────────────────────────────────────────────────


async def test_allocation_creates_sellable_codes_priced_now(client):
    t = await tenant()
    body = await allocate(client, t, 3)
    assert (body["quantity"], body["unit_price_ghs"], body["redeem_by"]) == (3, 10.0, None)
    assert len(set(body["codes"])) == 3
    for code in body["codes"]:
        v = await voucher_by_code(code)
        assert (v.source, v.status, v.expires_at, v.isp_operator_id) == ("reseller", "unused", None, t["operator"].id)
        assert (await portal_login(client, code, t["router"])).status_code == 200
    [row] = await audit_rows("reseller.allocate", t["reseller"].id)
    assert row.detail["quantity"] == 3 and row.detail["unit_price_ghs"] == "10.00"
    assert not any(code in str(row.detail) for code in body["codes"]), "codes are bearer credit: never in the trail"


async def test_sell_by_from_the_request_or_the_operator_default(client):
    t = await tenant(reseller_redeem_days=30)
    default = await allocate(client, t, 1)
    explicit = await allocate(client, t, 1, redeem_by=(now() + timedelta(days=2)).isoformat())
    none = await allocate(client, t, 1, redeem_by="")
    v_default = await voucher_by_code(default["codes"][0])
    assert timedelta(days=29, hours=23) < v_default.redeem_by - now() <= timedelta(days=30)
    assert timedelta(days=1, hours=23) < (await voucher_by_code(explicit["codes"][0])).redeem_by - now() <= timedelta(days=2)
    assert (await voucher_by_code(none["codes"][0])).redeem_by is None


@pytest.mark.parametrize("body,status", [
    ({"quantity": 0}, 400), ({"quantity": 1001}, 400), ({"quantity": "3"}, 400),
    ({"redeem_by": "2000-01-01T00:00:00Z"}, 400), ({"redeem_by": "tomorrow"}, 400),
    ({"plan_id": "not-a-uuid"}, 404),
])
async def test_bad_allocations_are_refused_and_nothing_is_made(client, body, status):
    t = await tenant()
    res = await client.post(f"/admin/resellers/{t['reseller'].id}/allocations", headers=t["h"],
                            json={"plan_id": str(t["plan"].id), "quantity": 2, **body})
    assert res.status_code == status, res.text
    assert (await client.get(f"/admin/resellers/{t['reseller'].id}/allocations", headers=t["h"])).json() == []


async def test_disabled_reseller_and_deactivated_plan_are_refused(client):
    t = await tenant()
    async with async_session_factory() as db:
        await db.execute(update(Plan).where(Plan.id == t["plan"].id).values(is_active=False))
        await db.commit()
    res = await client.post(f"/admin/resellers/{t['reseller'].id}/allocations", headers=t["h"],
                            json={"plan_id": str(t["plan"].id), "quantity": 1})
    assert res.status_code == 409
    t2 = await tenant()
    assert (await client.put(f"/admin/resellers/{t2['reseller'].id}", headers=t2["h"], json={"is_active": False})).status_code == 200
    res = await client.post(f"/admin/resellers/{t2['reseller'].id}/allocations", headers=t2["h"],
                            json={"plan_id": str(t2["plan"].id), "quantity": 1})
    assert res.status_code == 409


async def test_suspended_operator_cant_allocate(client):
    t = await tenant()
    async with async_session_factory() as db:
        await db.execute(update(ISPOperator).where(ISPOperator.id == t["operator"].id)
                         .values(status="suspended", billing_status="paused", suspension_reason="manual"))
        await db.commit()
    res = await client.post(f"/admin/resellers/{t['reseller'].id}/allocations", headers=t["h"],
                            json={"plan_id": str(t["plan"].id), "quantity": 1})
    assert res.status_code == 403


# ── counts ────────────────────────────────────────────────────────────────


async def test_counts_and_values_match_for_operator_and_reseller(client):
    t = await tenant()
    codes = (await allocate(client, t, 6))["codes"]
    vouchers = [await voucher_by_code(c) for c in codes]
    await accounting_start(vouchers[0], t["router"])  # redeemed
    async with async_session_factory() as db:
        await db.execute(update(Voucher).where(Voucher.id == vouchers[1].id).values(redeem_by=now() - timedelta(seconds=1)))
        await db.execute(update(Voucher).where(Voucher.id == vouchers[2].id).values(status="disabled"))  # vouchers page
        await db.commit()
    res = await client.post(f"/admin/resellers/{t['reseller'].id}/reclaim", headers=t["h"],
                            json={"voucher_ids": [str(vouchers[3].id)], "reason": "Lost slip"})
    assert res.json() == {"reclaimed": 1}

    operator_view = (await client.get(f"/admin/resellers/{t['reseller'].id}", headers=t["h"])).json()
    reseller_view = (await client.get("/reseller/summary", headers=await reseller_headers(client, t["reseller"]))).json()
    for view in (operator_view, reseller_view):
        totals = view["totals"]
        assert counts(totals) == {"allocated": 6, "remaining": 2, "redeemed": 1, "expired": 1, "reclaimed": 1, "disabled": 1}
        assert (totals["remaining_value_ghs"], totals["value_sold_ghs"], totals["commission_ghs"], totals["owed_ghs"]) == (20.0, 10.0, 0.0, 10.0)
        assert (totals["outstanding_ghs"], totals["credit_ghs"], totals["paid_ghs"]) == (10.0, 0.0, 0.0)
        [plan_row] = view["plans"]
        assert plan_row["plan_id"] == str(t["plan"].id) and plan_row["allocated"] == 6

    rows = (await client.get(f"/admin/resellers/{t['reseller'].id}/codes", headers=t["h"])).json()
    by_id = {r["voucher_id"]: r["status"] for r in rows}
    assert [by_id[str(v.id)] for v in vouchers] == ["redeemed", "expired", "disabled", "reclaimed", "remaining", "remaining"]
    only_remaining = (await client.get(f"/admin/resellers/{t['reseller'].id}/codes?status=remaining", headers=t["h"])).json()
    assert {r["voucher_id"] for r in only_remaining} == {str(vouchers[4].id), str(vouchers[5].id)}


# ── reclaim ───────────────────────────────────────────────────────────────


async def test_reclaim_by_allocation_takes_every_remaining_code_and_stops_them_at_once(client):
    t = await tenant()
    body = await allocate(client, t, 4)
    vouchers = [await voucher_by_code(c) for c in body["codes"]]
    await accounting_start(vouchers[0], t["router"])
    res = await client.post(f"/admin/resellers/{t['reseller'].id}/reclaim", headers=t["h"],
                            json={"allocation_id": body["allocation_id"], "reason": "Agent stopped selling"})
    assert res.json() == {"reclaimed": 3}
    for v in vouchers[1:]:
        res = await portal_login(client, v.code, t["router"])
        assert (res.status_code, res.json()["detail"]) == (400, DISABLED_DETAIL)
        assert not await radius_accepts(v, t["router"])
    assert (await fresh(Voucher, vouchers[0].id)).status == "active", "a redeemed code is the customer's"
    [row] = await audit_rows("reseller.reclaim", t["reseller"].id)
    assert row.detail["count"] == 3 and row.detail["reason"] == "Agent stopped selling"
    assert all(v.code not in str(row.detail) for v in vouchers)


async def test_reclaim_by_ids_is_all_or_nothing(client):
    """Negative: a redeemed code among the named ones: nothing is reclaimed."""
    t = await tenant()
    vouchers = [await voucher_by_code(c) for c in (await allocate(client, t, 2))["codes"]]
    await accounting_start(vouchers[0], t["router"])
    res = await client.post(f"/admin/resellers/{t['reseller'].id}/reclaim", headers=t["h"],
                            json={"voucher_ids": [str(v.id) for v in vouchers], "reason": "x"})
    assert res.status_code == 409
    assert (await fresh(Voucher, vouchers[1].id)).status == "unused"
    # Reclaimed codes are dead for good: a second reclaim finds nothing to take.
    assert (await client.post(f"/admin/resellers/{t['reseller'].id}/reclaim", headers=t["h"],
                              json={"voucher_ids": [str(vouchers[1].id)], "reason": "x"})).json() == {"reclaimed": 1}
    assert (await client.post(f"/admin/resellers/{t['reseller'].id}/reclaim", headers=t["h"],
                              json={"voucher_ids": [str(vouchers[1].id)], "reason": "x"})).status_code == 409


@pytest.mark.parametrize("case", ["no target", "both targets", "blank reason"])
async def test_reclaim_needs_one_target_and_a_reason(client, case):
    t = await tenant()
    body = await allocate(client, t, 1)
    voucher_id = str((await voucher_by_code(body["codes"][0])).id)
    payload = {
        "no target": {"reason": "x"},
        "both targets": {"voucher_ids": [voucher_id], "allocation_id": body["allocation_id"], "reason": "x"},
        "blank reason": {"voucher_ids": [voucher_id], "reason": "   "},
    }[case]
    res = await client.post(f"/admin/resellers/{t['reseller'].id}/reclaim", headers=t["h"], json=payload)
    assert res.status_code == 400, res.text
    assert (await fresh(Voucher, uuid.UUID(voucher_id))).status == "unused"


# ── isolation ─────────────────────────────────────────────────────────────


async def test_operator_a_cant_see_allocate_to_or_reclaim_from_operator_b(client):
    a, b = await tenant(), await tenant()
    b_codes = (await allocate(client, b, 2))
    b_rid = b["reseller"].id
    for method, path, body in (
        ("GET", f"/admin/resellers/{b_rid}", None),
        ("GET", f"/admin/resellers/{b_rid}/codes", None),
        ("GET", f"/admin/resellers/{b_rid}/allocations", None),
        ("GET", f"/admin/resellers/{b_rid}/account", None),
        ("POST", f"/admin/resellers/{b_rid}/allocations", {"plan_id": str(b["plan"].id), "quantity": 1}),
        ("POST", f"/admin/resellers/{b_rid}/reclaim", {"allocation_id": b_codes["allocation_id"], "reason": "x"}),
        ("PUT", f"/admin/resellers/{b_rid}", {"name": "Hijacked"}),
    ):
        res = await client.request(method, path, headers=a["h"], json=body)
        assert res.status_code == 404, (method, path, res.status_code)
    # B's plan for A's own reseller; and B's codes named through A's reseller.
    res = await client.post(f"/admin/resellers/{a['reseller'].id}/allocations", headers=a["h"],
                            json={"plan_id": str(b["plan"].id), "quantity": 1})
    assert res.status_code == 404
    b_ids = [str((await voucher_by_code(c)).id) for c in b_codes["codes"]]
    res = await client.post(f"/admin/resellers/{a['reseller'].id}/reclaim", headers=a["h"],
                            json={"voucher_ids": b_ids, "reason": "x"})
    assert res.status_code == 409
    res = await client.post(f"/admin/resellers/{a['reseller'].id}/reclaim", headers=a["h"],
                            json={"allocation_id": b_codes["allocation_id"], "reason": "x"})
    assert res.json() == {"reclaimed": 0}
    for code in b_codes["codes"]:
        assert (await voucher_by_code(code)).status == "unused"
    listed = (await client.get("/admin/resellers", headers=a["h"])).json()
    assert [r["id"] for r in listed] == [str(a["reseller"].id)]


async def test_a_reseller_sees_only_its_own_codes(client):
    t = await tenant()
    other = await make_reseller(t["operator"])
    mine = (await allocate(client, t, 2))["codes"]
    theirs_t = {**t, "reseller": other}
    theirs = (await allocate(client, theirs_t, 2))
    headers = await reseller_headers(client, t["reseller"])
    rows = (await client.get("/reseller/codes", headers=headers)).json()
    assert sorted(r["code"] for r in rows) == sorted(mine)
    # Another reseller's allocation id as a filter gives nothing.
    assert (await client.get(f"/reseller/codes?allocation_id={theirs['allocation_id']}", headers=headers)).json() == []
    summary = (await client.get("/reseller/summary", headers=headers)).json()
    assert summary["totals"]["allocated"] == 2


async def test_reseller_routes_take_no_admin_token_and_admin_routes_no_reseller_token(client):
    t = await tenant()
    rh = await reseller_headers(client, t["reseller"])
    assert (await client.get("/reseller/codes", headers=t["h"])).status_code == 401
    assert (await client.get(f"/admin/resellers/{t['reseller'].id}/codes", headers=rh)).status_code == 401
    res = await client.post(f"/admin/resellers/{t['reseller'].id}/allocations", headers=rh,
                            json={"plan_id": str(t["plan"].id), "quantity": 1})
    assert res.status_code == 401


async def test_staff_cant_reach_reseller_management(client):
    t = await tenant()
    _, staff = await make_admin(t["operator"], role="staff")
    rid = t["reseller"].id
    for method, path, body in (
        ("GET", "/admin/resellers", None), ("GET", f"/admin/resellers/{rid}/codes", None),
        ("POST", f"/admin/resellers/{rid}/allocations", {"plan_id": str(t["plan"].id), "quantity": 1}),
        ("POST", f"/admin/resellers/{rid}/reclaim", {"allocation_id": str(uuid.uuid4()), "reason": "x"}),
        ("GET", "/admin/reseller-settings", None),
    ):
        assert (await client.request(method, path, headers=staff, json=body)).status_code == 403, path


# ── disabling a reseller ──────────────────────────────────────────────────


async def test_disabling_a_reseller_ends_its_sessions_but_keeps_its_codes_working(client):
    t = await tenant()
    code = (await allocate(client, t, 1))["codes"][0]
    headers = await reseller_headers(client, t["reseller"])
    assert (await client.get("/reseller/me", headers=headers)).status_code == 200
    assert (await client.put(f"/admin/resellers/{t['reseller'].id}", headers=t["h"], json={"is_active": False})).status_code == 200
    assert (await client.get("/reseller/me", headers=headers)).status_code == 401
    # Re-enabled, the old token is still dead: the version moved on.
    assert (await client.put(f"/admin/resellers/{t['reseller'].id}", headers=t["h"], json={"is_active": True})).status_code == 200
    assert (await client.get("/reseller/me", headers=headers)).status_code == 401
    # The code may already be sold: it still works.
    assert (await portal_login(client, code, t["router"])).status_code == 200


async def test_a_password_change_ends_reseller_sessions(client):
    t = await tenant()
    login = (await client.post("/reseller/auth/login",
                               json={"email": t["reseller"].email, "password": "Str0ngPassword"})).json()
    assert (await client.put(f"/admin/resellers/{t['reseller'].id}", headers=t["h"],
                             json={"password": "An0ther-Str0ng"})).status_code == 200
    assert (await client.get("/reseller/me", headers={"Authorization": f"Bearer {login['access_token']}"})).status_code == 401
    assert (await client.post("/reseller/auth/refresh", json={"refresh_token": login["refresh_token"]})).status_code == 401


async def test_allocation_snapshot_survives_a_price_change(client):
    t = await tenant()
    body = await allocate(client, t, 1)
    async with async_session_factory() as db:
        await db.execute(update(Plan).where(Plan.id == t["plan"].id).values(price_ghs=Decimal("25.00")))
        await db.commit()
        allocation = await db.get(ResellerAllocation, uuid.UUID(body["allocation_id"]))
    assert allocation.unit_price_ghs == Decimal("10.00")
    totals = (await client.get(f"/admin/resellers/{t['reseller'].id}", headers=t["h"])).json()["totals"]
    assert totals["remaining_value_ghs"] == 10.0
