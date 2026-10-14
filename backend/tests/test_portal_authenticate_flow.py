"""The captive portal's voucher check, POST /portal/authenticate, against a REAL
database: it hands out a voucher's router password only when RADIUS would
accept that voucher on that router, and otherwise refuses with a message
saying why: invalid code, expired, fully used or disabled, and RADIUS's own
generic reject reply when the operator's service has ended or the data is
inconsistent. (A code the portal passes but RADIUS rejects shows as a silent
reload of the login page: the router sends the client back here without the
reason.)

Every case also runs RADIUS's own authorize_check_query, read from
freeradius/sql.conf, on the same rows as that router would send it (User-Name =
the voucher's username, NAS-IP-Address = the router's wg_tunnel_ip), and
requires the two to agree, including the password. So a change to either side
that the other doesn't follow fails here.

Runs the FastAPI app in-process. Needs the repo's freeradius/ directory
(mounted at /freeradius when the backend dir is mounted alone) and a
disposable database at migration 059 or later:

    PORTAL_AUTHENTICATE_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("PORTAL_AUTHENTICATE_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs PORTAL_AUTHENTICATE_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import text, update

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import ISPOperator, Plan, Router, Site, Town, Voucher
    from src.portal.routes import (
        DISABLED_DETAIL, EXPIRED_DETAIL, INVALID_CODE_DETAIL, USED_UP_DETAIL, VOUCHER_REFUSED_DETAIL,
    )
    from src.utils.encryption import encrypt_secret
    from src.utils.portal_token import create_portal_router_token


def _freeradius_dir() -> Path:
    for candidate in (os.getenv("FREERADIUS_DIR"), "/freeradius", Path(__file__).resolve().parents[2] / "freeradius"):
        if candidate and (Path(candidate) / "sql.conf").is_file():
            return Path(candidate)
    pytest.fail("freeradius/sql.conf not found: mount the repo's freeradius/ at /freeradius or set FREERADIUS_DIR")


def _radius_check_query() -> str:
    """authorize_check_query from sql.conf, with its two xlat expansions as bind
    parameters. Fails if anything else is expanded, so a new condition on
    another attribute can't be skipped silently."""
    conf = (_freeradius_dir() / "sql.conf").read_text()
    match = re.search(r'^\s*authorize_check_query\s*=\s*"(.*)"\s*$', conf, re.M)
    assert match, "authorize_check_query not found in sql.conf"
    query = match.group(1)
    query = query.replace("'%{NAS-IP-Address}'::inet", "CAST(:nas AS inet)")
    query = query.replace("'%{SQL-User-Name}'", "CAST(:user AS text)")
    assert "%{" not in query, f"unhandled expansion in authorize_check_query: {query}"
    return query


def _now():
    return datetime.now(timezone.utc)


@pytest_asyncio.fixture(loop_scope="module")
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ── fixtures ──────────────────────────────────────────────────────────────

def _state(status: str, ends_in: timedelta | None) -> dict:
    """Column values that satisfy 058's CHECKs. ends_in: wind-down end relative
    to now (cancelled only)."""
    if status == "cancelled":
        now = _now()
        return {"status": "cancelled", "billing_status": "cancelled", "cancelled_at": now - timedelta(days=1),
                "hotspot_service_ends_at": now + ends_in}
    return {"status": status, "billing_status": "active"}


async def make_operator(status="approved", ends_in: timedelta | None = None) -> "ISPOperator":
    slug = f"pa-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Pa {slug}", slug=slug, contact_email=f"{slug}@throwaway.test",
                               monthly_fee_ghs=Decimal("50.00"), **_state(status, ends_in))
        db.add(operator)
        await db.commit()
        return operator


# 10.101.x.2: clear of the seed data's 10.100.0.x routers.
_TUNNEL = iter(range(10, 250))


async def make_router(operator) -> "Router":
    async with async_session_factory() as db:
        town = Town(isp_operator_id=operator.id, name=f"T {uuid.uuid4().hex[:6]}", region="Volta")
        db.add(town)
        await db.flush()
        site = Site(isp_operator_id=operator.id, town_id=town.id, name="S", address="1 Test Road")
        db.add(site)
        await db.flush()
        router = Router(isp_operator_id=operator.id, site_id=site.id, name=f"R-{uuid.uuid4().hex[:6]}",
                        nas_identifier=f"nas-{uuid.uuid4().hex[:6]}", nas_secret=encrypt_secret("rt-secret"),
                        wg_tunnel_ip=f"10.101.{next(_TUNNEL)}.2", is_active=True, is_online=True)
        db.add(router)
        await db.commit()
        return router


async def make_plan(operator, *, data_limit_mb=None, is_active=True) -> "Plan":
    async with async_session_factory() as db:
        plan = Plan(isp_operator_id=operator.id, name=f"P {uuid.uuid4().hex[:6]}",
                    type="data" if data_limit_mb else "time", download_speed_kbps=2048, upload_speed_kbps=1024,
                    price_ghs=Decimal("5"), duration_minutes=None if data_limit_mb else 60,
                    data_limit_mb=data_limit_mb, is_active=is_active)
        db.add(plan)
        await db.commit()
        return plan


async def make_voucher(operator, plan, **fields) -> "Voucher":
    tag = uuid.uuid4().hex[:8].upper()
    async with async_session_factory() as db:
        # vouchers.code and username are unique table-wide: tag them.
        voucher = Voucher(isp_operator_id=operator.id, plan_id=plan.id, code=f"PA-{tag}", username=f"pa{tag.lower()}",
                          password=f"pw-{tag}", **{"status": "unused", **fields})
        db.add(voucher)
        await db.commit()
        return voucher


async def authenticate(client, router, code):
    return await client.post("/portal/authenticate", data={
        "code": code, "rt": create_portal_router_token(str(router.id)), "gateway": "10.5.50.1",
    })


async def radius_password(router, user) -> str | None:
    """What RADIUS's authorize_check_query gives that router for that user:
    the Cleartext-Password it checks against, or None (Access-Reject)."""
    async with async_session_factory() as db:
        row = (await db.execute(text(_radius_check_query()), {"nas": str(router.wg_tunnel_ip), "user": user})).first()
    return row.value if row else None


async def assert_accepted(client, router, voucher, code=None):
    resp = await authenticate(client, router, code or voucher.code)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True and body["username"] == voucher.username and body["password"] == voucher.password
    # RADIUS accepts what the portal handed out: the router logs in with this.
    assert await radius_password(router, body["username"]) == voucher.password


async def assert_refused(client, router, code, radius_user, detail):
    resp = await authenticate(client, router, code)
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": detail}  # and no password
    assert await radius_password(router, radius_user) is None, "the portal refuses a voucher RADIUS accepts"


# ── accepted: RADIUS accepts these, so the portal must too ────────────────

async def test_unused_voucher_is_accepted(client):
    operator = await make_operator()
    router = await make_router(operator)
    await assert_accepted(client, router, await make_voucher(operator, await make_plan(operator)))


async def test_active_voucher_with_time_left_is_accepted(client):
    operator = await make_operator()
    router = await make_router(operator)
    voucher = await make_voucher(operator, await make_plan(operator), status="active",
                                 activated_at=_now() - timedelta(minutes=10), expires_at=_now() + timedelta(minutes=50))
    await assert_accepted(client, router, voucher)


async def test_voucher_with_data_left_is_accepted(client):
    operator = await make_operator()
    router = await make_router(operator)
    voucher = await make_voucher(operator, await make_plan(operator, data_limit_mb=100), status="active",
                                 data_used_mb=99)
    await assert_accepted(client, router, voucher)


async def test_operator_in_wind_down_is_accepted(client):
    # Cancelled but the wind-down hasn't ended: service continues until it does.
    operator = await make_operator("cancelled", ends_in=timedelta(hours=2))
    router = await make_router(operator)
    await assert_accepted(client, router, await make_voucher(operator, await make_plan(operator)))


async def test_code_is_trimmed_and_upper_cased(client):
    operator = await make_operator()
    router = await make_router(operator)
    voucher = await make_voucher(operator, await make_plan(operator))
    await assert_accepted(client, router, voucher, code=f"  {voucher.code.lower()} ")


async def test_each_code_gets_its_own_voucher(client):
    operator = await make_operator()
    router = await make_router(operator)
    plan = await make_plan(operator)
    for voucher in [await make_voucher(operator, plan) for _ in range(3)]:
        await assert_accepted(client, router, voucher)


# ── invalid code: unknown, or another operator's ──────────────────────────

async def test_unknown_code_is_invalid(client):
    operator = await make_operator()
    router = await make_router(operator)
    await make_voucher(operator, await make_plan(operator))  # the operator does have valid codes
    code = f"PA-NOPE{uuid.uuid4().hex[:6].upper()}"
    await assert_refused(client, router, code, code, INVALID_CODE_DETAIL)


async def test_another_operators_voucher_is_invalid(client):
    operator, other = await make_operator(), await make_operator()
    router = await make_router(operator)
    voucher = await make_voucher(other, await make_plan(other))
    # Valid on the other operator's own routers: only the operator differs.
    await assert_accepted(client, await make_router(other), voucher)
    await assert_refused(client, router, voucher.code, voucher.username, INVALID_CODE_DETAIL)


async def test_another_operators_code_reads_exactly_like_an_unknown_one(client):
    """So the portal doesn't say which codes exist at other operators."""
    operator, other = await make_operator(), await make_operator()
    router = await make_router(operator)
    elsewhere = await make_voucher(other, await make_plan(other))
    responses = [await authenticate(client, router, c) for c in (elsewhere.code, "PA-NO-SUCH-CODE")]
    assert {(r.status_code, r.text) for r in responses} == {(400, f'{{"detail":"{INVALID_CODE_DETAIL}"}}')}


# ── expired: marked, or past its end before expire_vouchers marks it ───────

async def test_expired_voucher_is_expired(client):
    operator = await make_operator()
    router = await make_router(operator)
    voucher = await make_voucher(operator, await make_plan(operator), status="expired",
                                 expires_at=_now() + timedelta(minutes=30))  # time left: the status alone refuses
    await assert_refused(client, router, voucher.code, voucher.username, EXPIRED_DETAIL)


async def test_active_voucher_past_its_end_is_expired(client):
    operator = await make_operator()
    router = await make_router(operator)
    voucher = await make_voucher(operator, await make_plan(operator), status="active",
                                 activated_at=_now() - timedelta(hours=1), expires_at=_now() - timedelta(seconds=5))
    await assert_refused(client, router, voucher.code, voucher.username, EXPIRED_DETAIL)


# ── fully used: marked exhausted, or its data used up ─────────────────────

async def test_exhausted_voucher_is_fully_used(client):
    operator = await make_operator()
    router = await make_router(operator)
    voucher = await make_voucher(operator, await make_plan(operator), status="exhausted",
                                 expires_at=_now() + timedelta(minutes=30))
    await assert_refused(client, router, voucher.code, voucher.username, USED_UP_DETAIL)


async def test_voucher_with_its_data_used_up_is_fully_used(client):
    operator = await make_operator()
    router = await make_router(operator)
    voucher = await make_voucher(operator, await make_plan(operator, data_limit_mb=100), status="active",
                                 data_used_mb=100)
    await assert_refused(client, router, voucher.code, voucher.username, USED_UP_DETAIL)


# ── disabled: the voucher, or its plan deactivated ────────────────────────

async def test_disabled_voucher_is_disabled(client):
    operator = await make_operator()
    router = await make_router(operator)
    voucher = await make_voucher(operator, await make_plan(operator), status="disabled",
                                 expires_at=_now() + timedelta(minutes=30))
    await assert_refused(client, router, voucher.code, voucher.username, DISABLED_DETAIL)


async def test_voucher_on_a_deactivated_plan_is_disabled(client):
    operator = await make_operator()
    router = await make_router(operator)
    voucher = await make_voucher(operator, await make_plan(operator, is_active=False))
    await assert_refused(client, router, voucher.code, voucher.username, DISABLED_DETAIL)


# ── generic: the operator's service has ended, or bad data ────────────────

async def test_cut_off_operator_gets_the_generic_refusal(client):
    operator = await make_operator("cancelled", ends_in=timedelta(minutes=-1))
    router = await make_router(operator)
    voucher = await make_voucher(operator, await make_plan(operator))
    await assert_refused(client, router, voucher.code, voucher.username, VOUCHER_REFUSED_DETAIL)


async def test_cut_off_comes_before_the_vouchers_own_state(client):
    # A disabled or used-up code at a cut-off operator: the customer can't fix
    # either, so they aren't told about the voucher.
    operator = await make_operator("cancelled", ends_in=timedelta(minutes=-1))
    router = await make_router(operator)
    plan = await make_plan(operator, data_limit_mb=100)
    for fields in ({"status": "disabled"}, {"status": "active", "data_used_mb": 100}):
        voucher = await make_voucher(operator, plan, **fields)
        await assert_refused(client, router, voucher.code, voucher.username, VOUCHER_REFUSED_DETAIL)


async def test_voucher_on_another_operators_plan_gets_the_generic_refusal(client):
    # Bad data: RADIUS requires the plan's operator to be the router's too.
    operator, other = await make_operator(), await make_operator()
    router = await make_router(operator)
    voucher = await make_voucher(operator, await make_plan(other))
    await assert_refused(client, router, voucher.code, voucher.username, VOUCHER_REFUSED_DETAIL)


async def test_data_plan_voucher_with_no_usage_recorded_gets_the_generic_refusal(client):
    # Bad data: data_used_mb NULL on a data plan. RADIUS's "used < limit" is
    # then NULL, so it rejects; the portal mustn't trip over it either.
    operator = await make_operator()
    router = await make_router(operator)
    voucher = await make_voucher(operator, await make_plan(operator, data_limit_mb=100))
    async with async_session_factory() as db:  # the ORM would leave it to the server default (0)
        await db.execute(update(Voucher).where(Voucher.id == voucher.id).values(data_used_mb=None))
        await db.commit()
    assert (await fresh_voucher(voucher)).data_used_mb is None
    await assert_refused(client, router, voucher.code, voucher.username, VOUCHER_REFUSED_DETAIL)


async def test_another_operators_voucher_on_this_operators_plan_is_invalid(client):
    # The other way round: the voucher isn't this operator's, so it reads as
    # an unknown code here (RADIUS rejects it for the same reason).
    operator, other = await make_operator(), await make_operator()
    router = await make_router(operator)
    voucher = await make_voucher(other, await make_plan(operator))
    await assert_refused(client, router, voucher.code, voucher.username, INVALID_CODE_DETAIL)


# ── the messages and the input checks ─────────────────────────────────────

async def test_generic_refusal_is_radius_reject_reply():
    site = (_freeradius_dir() / "sites-enabled" / "default").read_text()
    assert f'Reply-Message := "{VOUCHER_REFUSED_DETAIL}"' in site


async def test_empty_code_keeps_its_own_message(client):
    operator = await make_operator()
    router = await make_router(operator)
    resp = await authenticate(client, router, "")
    assert resp.status_code == 400 and resp.json() == {"detail": "Please enter a voucher code"}


async def fresh_voucher(voucher) -> "Voucher":
    async with async_session_factory() as db:
        return await db.get(Voucher, voucher.id)
