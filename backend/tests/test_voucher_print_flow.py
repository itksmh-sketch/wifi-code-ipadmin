"""Voucher source (migration 050) and the two-step print batch, against a REAL database.

Covers why vouchers.source exists — a voucher already sold online, or allocated
to a reseller, must never be listed or printed as the operator's own stock —
and the print contract: fetching a batch marks nothing; only an explicit
confirm stamps printed_at, and only on vouchers still printable at that moment.

Runs the FastAPI app in-process. It writes operators, plans, vouchers and
payments, so it only runs when explicitly pointed at a disposable database, the
same gate as the other *_flow suites:

    ADMIN_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>

and skips otherwise — including in the production container. Run one *_flow
module per pytest invocation: they share the app's global engine, and a second
module's event loop cannot reuse the first module's pooled connections.
"""
from __future__ import annotations

import importlib
import os
import uuid
from datetime import datetime, timedelta, timezone

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
    from src.modules.payments.service import PaymentService
    from src.modules.vouchers.engine import generate_vouchers
    from src.schemas import DevicePolicy, VoucherGenerate

    MIGRATION_050 = importlib.import_module("src.db.migrations.versions.050_voucher_source_and_printed_at")


async def sql(query, **params):
    async with async_session_factory() as db:
        result = await db.execute(text(query), params)
        await db.commit()
        return result


async def make_operator():
    """A fresh operator with a town, site, plan and reseller. Returns their ids."""
    ids = {k: uuid.uuid4() for k in ("op", "town", "site", "plan", "reseller")}
    slug = f"vp-{ids['op'].hex[:10]}"
    await sql(
        "insert into isp_operators (id,name,slug,contact_email,status) values (:op,:n,:n,:e,'approved')",
        op=ids["op"], n=slug, e=f"{slug}@throwaway.test",
    )
    await sql("insert into towns (id,name,region,isp_operator_id) values (:t,'T','Volta',:op)", t=ids["town"], op=ids["op"])
    await sql(
        "insert into sites (id,town_id,name,address,isp_operator_id) values (:s,:t,'S','addr',:op)",
        s=ids["site"], t=ids["town"], op=ids["op"],
    )
    await sql(
        "insert into plans (id,name,type,duration_minutes,download_speed_kbps,upload_speed_kbps,price_ghs,isp_operator_id)"
        " values (:p,'Daily','time',1440,1024,512,5,:op)",
        p=ids["plan"], op=ids["op"],
    )
    await sql(
        "insert into resellers (id,name,email,password_hash,role,isp_operator_id) values (:r,'R',:e,'x','reseller',:op)",
        r=ids["reseller"], e=f"r-{slug}@throwaway.test", op=ids["op"],
    )
    return ids


def tag(ids, name):
    """A code unique to this operator. vouchers.code is unique table-wide, so a
    fixed code would make the suite fail the second time it runs."""
    return f"{name}-{ids['op'].hex[:8]}"


async def make_voucher(ids, *, code=None, source="manual", status="unused", batch_id=None, created_at=None):
    vid = uuid.uuid4()
    await sql(
        "insert into vouchers (id,isp_operator_id,plan_id,site_id,code,username,password,status,source,batch_id,created_at)"
        " values (:v,:op,:p,:s,:c,:u,'secret-pw',:st,:src,:b,coalesce(:ca, now()))",
        v=vid, op=ids["op"], p=ids["plan"], s=ids["site"], c=code or f"C-{vid.hex[:12]}", u=f"u-{vid.hex[:12]}",
        st=status, src=source, b=batch_id, ca=created_at,
    )
    return vid


async def link_payment(ids, voucher_id, *, status="success", diagnostic=False):
    await sql(
        "insert into payment_transactions (plan_id,site_id,amount_ghs,payment_method,provider,internal_reference,"
        "isp_operator_id,status,is_diagnostic,voucher_id) values (:p,:s,5,'mtn_momo','paystack',:ref,:op,:st,:d,:v)",
        p=ids["plan"], s=ids["site"], ref=f"ref-{uuid.uuid4().hex}", op=ids["op"], st=status, d=diagnostic, v=voucher_id,
    )


async def allocate_to_reseller(ids, voucher_id):
    await sql(
        "insert into reseller_voucher_allocations (reseller_id,voucher_id,purchase_price_ghs) values (:r,:v,4)",
        r=ids["reseller"], v=voucher_id,
    )


async def source_of(voucher_id):
    return (await sql("select source from vouchers where id=:v", v=voucher_id)).scalar_one()


@pytest_asyncio.fixture(loop_scope="module")
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as c:
        yield c
    app.dependency_overrides.clear()


def act_as(ids):
    """Every request after this is made as an admin of that operator."""
    app.dependency_overrides[get_admin_tenant_context] = lambda: TenantContext(
        False, ids["op"], uuid.uuid4(), "superadmin", "t@throwaway.test"
    )


# ── Migration 050 backfill ────────────────────────────────────────────────


async def run_050_backfill():
    await sql(MIGRATION_050.BACKFILL_ONLINE)
    await sql(MIGRATION_050.BACKFILL_RESELLER)


async def test_backfill_follows_links_not_batch_prefixes():
    ids = await make_operator()
    # Every row starts as 'manual', exactly as the column default leaves it.
    plain = await make_voucher(ids, batch_id="9b81abcd")
    decoy = await make_voucher(ids, batch_id="PAY-decoy000")            # prefix, no link
    sold = await make_voucher(ids, batch_id="PAY-f081f6c9")
    await link_payment(ids, sold, status="success")
    reversed_diag = await make_voucher(ids, batch_id="diagnostic-void-2026-09-09")
    await link_payment(ids, reversed_diag, status="reversed", diagnostic=True)
    reseller = await make_voucher(ids, batch_id="SEED-RESELLER")        # no RES- prefix
    await allocate_to_reseller(ids, reseller)
    both = await make_voucher(ids)
    await link_payment(ids, both)
    await allocate_to_reseller(ids, both)

    await run_050_backfill()

    assert await source_of(plain) == "manual"
    assert await source_of(decoy) == "manual", "a PAY- prefix without a payment link is not a sale"
    assert await source_of(sold) == "online"
    assert await source_of(reversed_diag) == "online", "any payment link, whatever its status, keeps it unprintable"
    assert await source_of(reseller) == "reseller", "reseller stock is found by allocation, not prefix"
    assert await source_of(both) == "online", "online wins when both links exist"


async def test_backfill_is_idempotent():
    ids = await make_operator()
    sold = await make_voucher(ids)
    await link_payment(ids, sold)
    await run_050_backfill()
    snapshot = (await sql("select id, source from vouchers where isp_operator_id=:op order by id", op=ids["op"])).all()
    await run_050_backfill()
    assert (await sql("select id, source from vouchers where isp_operator_id=:op order by id", op=ids["op"])).all() == snapshot


async def test_source_check_constraint_rejects_unknown_values():
    ids = await make_operator()
    vid = await make_voucher(ids)
    with pytest.raises(Exception, match="ck_vouchers_source"):
        await sql("update vouchers set source='bogus' where id=:v", v=vid)


# ── Creation paths set source explicitly ──────────────────────────────────


async def test_operator_batch_generation_is_manual():
    ids = await make_operator()
    async with async_session_factory() as db:
        vouchers, _ = await generate_vouchers(
            db,
            VoucherGenerate(plan_id=ids["plan"], site_id=ids["site"], quantity=3, device_policy=DevicePolicy.single),
            ids["op"],
        )
        await db.commit()
        assert {v.source for v in vouchers} == {"manual"}


async def test_online_purchase_is_online():
    ids = await make_operator()
    ref = f"vp-{uuid.uuid4().hex[:12]}"
    await sql(
        "insert into payment_transactions (plan_id,site_id,amount_ghs,payment_method,provider,internal_reference,"
        "isp_operator_id,status) values (:p,:s,5,'mtn_momo','paystack',:ref,:op,'pending')",
        p=ids["plan"], s=ids["site"], ref=ref, op=ids["op"],
    )
    async with async_session_factory() as db:
        tx = await PaymentService().resolve_successful_payment(db, internal_reference=ref, trigger_source="test")
    assert tx.status == "success"
    assert await source_of(tx.voucher_id) == "online"


# ── GET /vouchers source filter ───────────────────────────────────────────


async def test_list_defaults_to_manual_and_all_overrides(client):
    ids = await make_operator()
    manual = await make_voucher(ids, code=tag(ids, "LIST-MANUAL"))
    sold = await make_voucher(ids, code=tag(ids, "LIST-ONLINE"), source="online")
    await link_payment(ids, sold)
    await make_voucher(ids, code=tag(ids, "LIST-RESELLER"), source="reseller")
    act_as(ids)

    codes = lambda res: {v["code"] for v in res.json()["vouchers"]}
    res = await client.get("/vouchers", params={"limit": 200})
    assert res.status_code == 200
    assert codes(res) == {tag(ids, "LIST-MANUAL")}, "the default list must not offer sold or reseller vouchers as stock"
    assert res.json()["vouchers"][0]["source"] == "manual"
    assert "printed_at" in res.json()["vouchers"][0]

    res = await client.get("/vouchers", params={"source": "all", "limit": 200})
    assert codes(res) == {tag(ids, n) for n in ("LIST-MANUAL", "LIST-ONLINE", "LIST-RESELLER")}
    res = await client.get("/vouchers", params={"source": "online", "status": "unused"})
    assert codes(res) == {tag(ids, "LIST-ONLINE")}, "an operator can still find a customer's purchased voucher"
    assert manual  # created for the default-list assertion above


async def test_list_rejects_unknown_source(client):
    act_as(await make_operator())
    assert (await client.get("/vouchers", params={"source": "bogus"})).status_code == 422


# ── GET /vouchers/print-batch ─────────────────────────────────────────────


async def printable_stock(ids, n=5):
    """n manual vouchers with increasing ages, oldest first."""
    base = datetime.now(timezone.utc) - timedelta(days=5)
    return [await make_voucher(ids, code=tag(ids, f"PRINT-{i}"), created_at=base + timedelta(hours=i)) for i in range(n)]


def printed_codes(ids, *indexes):
    return [tag(ids, f"PRINT-{i}") for i in indexes]


async def test_print_batch_returns_only_printable_manual_stock_oldest_first(client):
    ids = await make_operator()
    stock = await printable_stock(ids)
    await make_voucher(ids, code=tag(ids, "SKIP-ONLINE"), source="online")
    await make_voucher(ids, code=tag(ids, "SKIP-RESELLER"), source="reseller")
    await make_voucher(ids, code=tag(ids, "SKIP-USED"), status="active")
    printed = await make_voucher(ids, code=tag(ids, "SKIP-PRINTED"))
    await sql("update vouchers set printed_at=now() where id=:v", v=printed)
    act_as(ids)

    res = await client.get("/vouchers/print-batch", params={"plan_id": str(ids["plan"])})
    assert res.status_code == 200
    body = res.json()
    assert [v["code"] for v in body["vouchers"]] == printed_codes(ids, *range(5))
    assert [v["id"] for v in body["vouchers"]] == [str(v) for v in stock]
    assert body["available"] == 5
    assert body["plan"]["id"] == str(ids["plan"]) and body["plan"]["name"] == "Daily"
    assert all(set(v) == {"id", "code", "created_at"} for v in body["vouchers"]), "no username/password on paper"


async def test_print_batch_limit_truncates_but_available_is_the_full_count(client):
    ids = await make_operator()
    await printable_stock(ids)
    act_as(ids)
    body = (await client.get("/vouchers/print-batch", params={"plan_id": str(ids["plan"]), "limit": 2})).json()
    assert [v["code"] for v in body["vouchers"]] == printed_codes(ids, 0, 1)
    assert body["available"] == 5


async def test_fetching_a_print_batch_marks_nothing(client):
    ids = await make_operator()
    await printable_stock(ids)
    act_as(ids)
    for _ in range(3):
        await client.get("/vouchers/print-batch", params={"plan_id": str(ids["plan"])})
    marked = (await sql("select count(*) from vouchers where isp_operator_id=:op and printed_at is not null", op=ids["op"])).scalar()
    assert marked == 0


async def test_print_batch_for_another_operators_plan_is_404(client):
    mine, theirs = await make_operator(), await make_operator()
    await printable_stock(theirs)
    act_as(mine)
    res = await client.get("/vouchers/print-batch", params={"plan_id": str(theirs["plan"])})
    assert res.status_code == 404


# ── POST /vouchers/print-batch/confirm ────────────────────────────────────


async def test_confirm_marks_exactly_the_still_printable_vouchers(client):
    ids, other = await make_operator(), await make_operator()
    stock = await printable_stock(ids, 3)
    used_since_fetch = stock[2]
    await sql("update vouchers set status='active' where id=:v", v=used_since_fetch)
    online = await make_voucher(ids, source="online")
    foreign = await make_voucher(other)
    unknown = uuid.uuid4()
    act_as(ids)

    requested = [str(v) for v in stock] + [str(stock[0]), str(online), str(foreign), str(unknown)]
    res = await client.post("/vouchers/print-batch/confirm", json={"voucher_ids": requested})
    assert res.status_code == 200
    body = res.json()
    assert body["confirmed"] == 2
    assert body["already_printed"] == []
    assert set(body["not_printable"]) == {str(used_since_fetch), str(online), str(foreign), str(unknown)}

    marked = {row[0] for row in (await sql("select id from vouchers where printed_at is not null and id = any(:ids)",
                                            ids=[stock[0], stock[1], stock[2], online, foreign])).all()}
    assert marked == {stock[0], stock[1]}, "only the two still-printable vouchers are stamped"


async def test_reconfirming_reports_already_printed_without_restamping(client):
    ids = await make_operator()
    stock = await printable_stock(ids, 2)
    act_as(ids)
    await client.post("/vouchers/print-batch/confirm", json={"voucher_ids": [str(v) for v in stock]})
    first = (await sql("select printed_at from vouchers where id=:v", v=stock[0])).scalar()

    body = (await client.post("/vouchers/print-batch/confirm", json={"voucher_ids": [str(v) for v in stock]})).json()
    assert body["confirmed"] == 0
    assert set(body["already_printed"]) == {str(v) for v in stock}
    assert (await sql("select printed_at from vouchers where id=:v", v=stock[0])).scalar() == first


async def test_the_next_print_run_skips_confirmed_vouchers(client):
    ids = await make_operator()
    stock = await printable_stock(ids, 4)
    act_as(ids)
    await client.post("/vouchers/print-batch/confirm", json={"voucher_ids": [str(stock[0]), str(stock[1])]})
    body = (await client.get("/vouchers/print-batch", params={"plan_id": str(ids["plan"])})).json()
    assert [v["code"] for v in body["vouchers"]] == printed_codes(ids, 2, 3)
    assert body["available"] == 2


async def test_confirm_with_no_ids_is_rejected(client):
    act_as(await make_operator())
    assert (await client.post("/vouchers/print-batch/confirm", json={"voucher_ids": []})).status_code == 422


async def test_print_batch_route_does_not_shadow_voucher_by_id(client):
    ids = await make_operator()
    vid = await make_voucher(ids)
    act_as(ids)
    res = await client.get(f"/vouchers/{vid}")
    assert res.status_code == 200 and res.json()["id"] == str(vid)
