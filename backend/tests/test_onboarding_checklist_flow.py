"""The Billing page's onboarding checklist, against a REAL database.

Pins three fixes:

* mark_checklist used to mutate the loaded JSONB dict and assign the same object
  back; SQLAlchemy saw no change and every step after an operator's first was
  silently lost. The persistence tests replay exactly that sequence.
* Migration 051 backfills lost steps from evidence, additively.
* portal_tested and first_sale_made are derived at read time, because the event
  they stand for — a customer getting online — is written by FreeRADIUS, not by
  application code. The seven derivation scenarios are the ones each phase was
  validated against.

It writes operators, sessions and payments, so it only runs when explicitly
pointed at a disposable database, the same gate as the other *_flow suites:

    ADMIN_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>

and skips otherwise — including in the production container. Run one *_flow
module per pytest invocation (they share the app's global engine).
"""
from __future__ import annotations

import importlib
import json
import os
import uuid

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("ADMIN_FLOW_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs ADMIN_FLOW_TEST_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import text

    from src.app import app
    from src.db.base import async_session_factory
    from src.middleware.auth import TenantContext, get_admin_tenant_context
    from src.modules.onboarding import CHECKLIST_KEYS, get_checklist, mark_checklist
    from src.modules.payments.service import PaymentService

    MIGRATION_051 = importlib.import_module("src.db.migrations.versions.051_backfill_onboarding_checklist")

STORED_STEPS = ["town_added", "router_added", "payment_configured", "voucher_generated", "first_sale_made"]


async def sql(query, **params):
    async with async_session_factory() as db:
        result = await db.execute(text(query), params)
        await db.commit()
        return result


async def make_operator(stored: dict | None = None, *, with_setup=True):
    """A fresh operator; with_setup adds a town, site, plan and router."""
    ids = {k: uuid.uuid4() for k in ("op", "town", "site", "plan", "router")}
    slug = f"ob-{ids['op'].hex[:10]}"
    await sql(
        "insert into isp_operators (id,name,slug,contact_email,status,onboarding_checklist)"
        " values (:op,:n,:n,:e,'approved',cast(:c as jsonb))",
        op=ids["op"], n=slug, e=f"{slug}@throwaway.test", c=json.dumps(stored or {}),
    )
    if with_setup:
        await sql("insert into towns (id,name,region,isp_operator_id) values (:t,'T','Volta',:op)", t=ids["town"], op=ids["op"])
        await sql(
            "insert into sites (id,town_id,name,address,isp_operator_id) values (:s,:t,'S','a',:op)",
            s=ids["site"], t=ids["town"], op=ids["op"],
        )
        await sql(
            "insert into plans (id,name,type,download_speed_kbps,upload_speed_kbps,price_ghs,isp_operator_id)"
            " values (:p,'P','time',1,1,1,:op)",
            p=ids["plan"], op=ids["op"],
        )
        await sql(
            "insert into routers (id,name,isp_operator_id,site_id,nas_identifier,nas_secret) values (:r,'R',:op,:s,:n,'x')",
            r=ids["router"], op=ids["op"], s=ids["site"], n=f"nas-{ids['router'].hex[:10]}",
        )
    return ids


async def make_voucher(ids, source):
    vid = uuid.uuid4()
    await sql(
        "insert into vouchers (id,isp_operator_id,plan_id,code,username,password,status,source)"
        " values (:v,:op,:p,:c,:c,'pw','active',:src)",
        v=vid, op=ids["op"], p=ids["plan"], c=f"c-{vid.hex[:12]}", src=source,
    )
    return vid


async def make_session(ids, voucher_id, *, owner=None):
    """A RADIUS session as FreeRADIUS would write it. `owner` is the operator
    whose router carried it (defaults to the voucher's operator)."""
    who = owner or ids
    await sql(
        "insert into sessions (isp_operator_id,voucher_id,router_id,username,nas_ip,session_id,started_at)"
        " values (:op,:v,:r,'u','10.100.0.9',:sid,now())",
        op=who["op"], v=voucher_id, r=who["router"], sid=f"s-{uuid.uuid4().hex}",
    )


async def make_payment(ids, *, diagnostic, status="success", voucher_id=None, ref=None):
    ref = ref or f"ob-{uuid.uuid4().hex}"
    await sql(
        "insert into payment_transactions (plan_id,site_id,amount_ghs,payment_method,provider,internal_reference,"
        "isp_operator_id,status,is_diagnostic,voucher_id) values (:p,:s,1,'card','paystack',:ref,:op,:st,:d,:v)",
        p=ids["plan"], s=ids["site"], ref=ref, op=ids["op"], st=status, d=diagnostic, v=voucher_id,
    )
    return ref


async def stored(ids) -> dict:
    return (await sql("select onboarding_checklist from isp_operators where id=:op", op=ids["op"])).scalar_one()


async def checklist(ids) -> dict:
    async with async_session_factory() as db:
        return await get_checklist(db, ids["op"])


# ── mark_checklist persistence (the reproduced bug) ───────────────────────


async def test_every_sequential_mark_persists():
    """One request-like session per mark, committed — how the routes call it.
    Before the fix only the first of these survived."""
    ids = await make_operator(with_setup=False)
    for count, key in enumerate(STORED_STEPS, start=1):
        async with async_session_factory() as db:
            await mark_checklist(db, ids["op"], key)
            await db.commit()
        now = await stored(ids)
        assert sorted(k for k, v in now.items() if v) == sorted(STORED_STEPS[:count]), f"lost a step after marking {key}"


async def test_marking_an_existing_step_again_changes_nothing():
    ids = await make_operator({"town_added": True, "router_added": True}, with_setup=False)
    async with async_session_factory() as db:
        await mark_checklist(db, ids["op"], "town_added")
        await db.commit()
    assert await stored(ids) == {"town_added": True, "router_added": True}


async def test_unknown_keys_are_ignored():
    ids = await make_operator(with_setup=False)
    async with async_session_factory() as db:
        await mark_checklist(db, ids["op"], "not_a_step")
        await db.commit()
    assert await stored(ids) == {}


# ── Migration 051 backfill ────────────────────────────────────────────────


async def test_backfill_restores_steps_from_evidence():
    # Shaped like the two live operators: everything done, but only one step stored.
    like_aflao = await make_operator({"town_added": True})
    like_tenant_zero = await make_operator({"voucher_generated": True})
    no_evidence = await make_operator({"portal_tested": True}, with_setup=False)
    for ids in (like_aflao, like_tenant_zero):
        await sql(
            "insert into operator_payment_credentials (isp_operator_id,provider,credentials_encrypted,is_active)"
            " values (:op,'paystack','x',true)",
            op=ids["op"],
        )
        await make_voucher(ids, "manual")
    await make_payment(like_aflao, diagnostic=False)
    await make_payment(like_tenant_zero, diagnostic=True)   # a test run is not a sale

    await sql(MIGRATION_051.BACKFILL)

    assert await stored(like_aflao) == {k: True for k in STORED_STEPS}
    assert await stored(like_tenant_zero) == {k: True for k in STORED_STEPS if k != "first_sale_made"}
    assert await stored(no_evidence) == {"portal_tested": True}, "an existing mark is never cleared"


async def test_backfill_never_writes_false_and_is_idempotent():
    await make_operator()
    await sql(MIGRATION_051.BACKFILL)
    falses = (await sql("select count(*) from isp_operators, jsonb_each(onboarding_checklist) e where e.value = 'false'::jsonb")).scalar()
    assert falses == 0
    before = (await sql("select md5(string_agg(onboarding_checklist::text, ',' order by id)) from isp_operators")).scalar()
    await sql(MIGRATION_051.BACKFILL)
    after = (await sql("select md5(string_agg(onboarding_checklist::text, ',' order by id)) from isp_operators")).scalar()
    assert before == after


# ── Read-time derivation: portal_tested and first_sale_made ───────────────


async def test_no_evidence_leaves_both_open():
    ids = await make_operator()
    c = await checklist(ids)
    assert (c["portal_tested"], c["first_sale_made"]) == (False, False)


async def test_a_used_printed_voucher_proves_the_portal_and_a_sale():
    ids = await make_operator()
    await make_session(ids, await make_voucher(ids, "manual"))
    c = await checklist(ids)
    assert (c["portal_tested"], c["first_sale_made"]) == (True, True)


async def test_a_reseller_voucher_proves_the_portal_but_is_not_the_operators_sale():
    ids = await make_operator()
    await make_session(ids, await make_voucher(ids, "reseller"))
    c = await checklist(ids)
    assert (c["portal_tested"], c["first_sale_made"]) == (True, False)


async def test_a_diagnostic_payment_is_not_a_sale_even_when_its_voucher_is_used():
    ids = await make_operator()
    voucher = await make_voucher(ids, "online")
    await make_payment(ids, diagnostic=True, voucher_id=voucher)
    await make_session(ids, voucher)
    c = await checklist(ids)
    assert (c["portal_tested"], c["first_sale_made"]) == (True, False)


async def test_a_real_online_payment_is_a_sale_before_anyone_is_online():
    ids = await make_operator()
    await make_payment(ids, diagnostic=False)
    c = await checklist(ids)
    assert (c["portal_tested"], c["first_sale_made"]) == (False, True)


async def test_another_operators_session_never_counts():
    mine, theirs = await make_operator(), await make_operator()
    await make_session(mine, await make_voucher(mine, "manual"), owner=theirs)
    c = await checklist(mine)
    assert (c["portal_tested"], c["first_sale_made"]) == (False, False)


async def test_stored_marks_still_count_without_evidence():
    ids = await make_operator({"portal_tested": True, "first_sale_made": True})
    c = await checklist(ids)
    assert (c["portal_tested"], c["first_sale_made"]) == (True, True)


async def test_derivation_never_writes_to_the_stored_checklist():
    ids = await make_operator({"town_added": True})
    await make_session(ids, await make_voucher(ids, "manual"))
    await checklist(ids)
    assert await stored(ids) == {"town_added": True}


# ── Live payment path: diagnostic payments are not a first sale ───────────


async def test_resolving_a_diagnostic_payment_does_not_mark_first_sale():
    ids = await make_operator()
    ref = await make_payment(ids, diagnostic=True, status="pending")
    async with async_session_factory() as db:
        tx = await PaymentService().resolve_successful_payment(db, internal_reference=ref, trigger_source="test")
    assert tx.status == "success"
    assert not (await stored(ids)).get("first_sale_made")


async def test_resolving_a_real_payment_marks_first_sale():
    ids = await make_operator()
    ref = await make_payment(ids, diagnostic=False, status="pending")
    async with async_session_factory() as db:
        await PaymentService().resolve_successful_payment(db, internal_reference=ref, trigger_source="test")
    assert (await stored(ids)).get("first_sale_made") is True


# ── The endpoint the Billing page reads ───────────────────────────────────


@pytest_asyncio.fixture(loop_scope="module")
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as c:
        yield c
    app.dependency_overrides.clear()


async def test_billing_endpoint_reports_a_complete_checklist(client):
    """Evidence for all six steps -> every value true, which is what hides the
    Getting Started panel (Billing.jsx: allChecked = every value true)."""
    ids = await make_operator({"payment_configured": True, "voucher_generated": True})
    await sql(MIGRATION_051.BACKFILL)
    await make_session(ids, await make_voucher(ids, "manual"))
    app.dependency_overrides[get_admin_tenant_context] = lambda: TenantContext(
        False, ids["op"], uuid.uuid4(), "superadmin", "t@throwaway.test"
    )
    res = await client.get("/billing/onboarding-checklist")
    assert res.status_code == 200
    body = res.json()
    assert set(body) == set(CHECKLIST_KEYS)
    assert all(body.values()), body
