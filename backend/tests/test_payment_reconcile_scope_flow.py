"""Manual payment reconciliation against a REAL database: POST
/payments/reconcile queues a run for the caller's own operator only, and that
run neither calls a provider for nor changes the status of another operator's
pending payments. The scheduled run (no operator) still covers every tenant.

Only runs against a disposable database:

    PAYMENT_RECONCILE_SCOPE_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>

(tests/throwaway_db.py run sets both.)
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("PAYMENT_RECONCILE_SCOPE_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs PAYMENT_RECONCILE_SCOPE_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import AdminUser, ISPOperator, PaymentTransaction, Plan, Site, Town
    from src.jobs.payment_reconciliation import run_payment_reconciliation
    from src.modules.auth.tokens import admin_token_response
    from src.modules.payments import routes as payment_routes
    from src.modules.payments.service import PaymentService
    from src.utils.auth import hash_password

PASSWORD = "Str0ngPassword"


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    queued: list[tuple[str, dict]] = []

    async def pool():
        async def enqueue_job(name, *args, **kwargs):
            assert not args
            queued.append((name, kwargs))
            return SimpleNamespace(job_id=f"job-{len(queued)}")
        return SimpleNamespace(enqueue_job=enqueue_job)

    monkeypatch.setattr(payment_routes, "get_redis_pool", pool)

    # Every provider lookup the job makes, by transaction; then fails it, so
    # no network is ever reached and the payment stays pending.
    looked_up: list[uuid.UUID] = []

    async def provider_for_transaction(self, db, tx):
        looked_up.append(tx.id)
        raise RuntimeError("no provider in tests")

    monkeypatch.setattr(PaymentService, "provider_for_transaction", provider_for_transaction)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as client:
        yield {"client": client, "queued": queued, "looked_up": looked_up}


async def make_tenant() -> dict:
    slug = f"rcn-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Rcn {slug}", slug=slug, contact_email=f"{slug}@throwaway.test", status="approved")
        db.add(operator)
        await db.flush()
        town = Town(isp_operator_id=operator.id, name="Town", region="Greater Accra")
        db.add(town)
        await db.flush()
        site = Site(isp_operator_id=operator.id, town_id=town.id, name="Site", address="1 Test Road")
        plan = Plan(isp_operator_id=operator.id, name="1h", type="time", duration_minutes=60,
                    download_speed_kbps=1024, upload_speed_kbps=512, price_ghs=Decimal("2.00"))
        admin = AdminUser(
            isp_operator_id=operator.id, email=f"adm-{uuid.uuid4().hex[:10]}@throwaway.test",
            password_hash=hash_password(PASSWORD), role="superadmin", is_active=True, phone="233244000777",
            phone_verified=True, must_complete_onboarding=False, must_change_password=False,
            security_question="birth_town", security_answer_hash=hash_password("accra"),
        )
        db.add_all([site, plan, admin])
        await db.commit()
    token = admin_token_response(admin).access_token
    return {"operator": operator, "site": site, "plan": plan, "headers": {"Authorization": f"Bearer {token}"}}


async def make_pending(t, *, age: timedelta, provider_reference: str | None) -> uuid.UUID:
    async with async_session_factory() as db:
        tx = PaymentTransaction(
            isp_operator_id=t["operator"].id, plan_id=t["plan"].id, site_id=t["site"].id, amount_ghs=Decimal("2.00"),
            payment_method="mtn_momo", provider="paystack", status="pending", provider_reference=provider_reference,
            internal_reference=f"INT-{uuid.uuid4().hex[:12]}", phone_number="233244000777",
            initiated_at=datetime.now(timezone.utc) - age,
        )
        db.add(tx)
        await db.commit()
        return tx.id


async def status_of(tx_id) -> str:
    async with async_session_factory() as db:
        return (await db.get(PaymentTransaction, tx_id)).status


async def stale_and_checkable(t) -> tuple[uuid.UUID, uuid.UUID]:
    """One that times out without a provider call (no reference, 3h old) and
    one the job must ask its provider about (reference, 20 min old)."""
    return (await make_pending(t, age=timedelta(hours=3), provider_reference=None),
            await make_pending(t, age=timedelta(minutes=20), provider_reference=f"PSK-{uuid.uuid4().hex[:10]}"))


async def test_route_queues_a_run_for_the_callers_operator_only(env):
    a, b = await make_tenant(), await make_tenant()
    env["queued"].clear()
    for t in (a, b):
        res = await env["client"].post("/payments/reconcile", headers=t["headers"])
        assert res.status_code == 200, res.text
    assert env["queued"] == [
        ("run_payment_reconciliation", {"isp_operator_id": str(a["operator"].id)}),
        ("run_payment_reconciliation", {"isp_operator_id": str(b["operator"].id)}),
    ]


async def test_route_never_queues_an_unscoped_run(env):
    """Negative: nothing an operator sends widens the run to every tenant."""
    a = await make_tenant()
    env["queued"].clear()
    res = await env["client"].post("/payments/reconcile?isp_operator_id=", headers=a["headers"],
                                   json={"isp_operator_id": None})
    assert res.status_code == 200
    [(_, kwargs)] = env["queued"]
    assert kwargs == {"isp_operator_id": str(a["operator"].id)}


@pytest.mark.parametrize("platform_owner", [False, True])
async def test_caller_without_an_operator_is_refused_and_nothing_is_queued(env, platform_owner):
    """Negative: no operator must never reach the job as None (which it reads
    as every tenant). Unreachable through real auth today — the column is NOT
    NULL and admin tokens must carry one — so the context is overridden."""
    from src.middleware.auth import TenantContext, get_admin_tenant_context

    a = await make_tenant()  # a real admin token, for the permission check
    app.dependency_overrides[get_admin_tenant_context] = lambda: TenantContext(
        is_platform_owner=platform_owner, isp_operator_id=None, user_id=uuid.uuid4(), role="superadmin",
        email="nobody@throwaway.test",
    )
    env["queued"].clear()
    try:
        res = await env["client"].post("/payments/reconcile", headers=a["headers"])
    finally:
        app.dependency_overrides.pop(get_admin_tenant_context, None)
    assert res.status_code == 403
    assert env["queued"] == []


async def test_platform_owner_token_is_refused_and_nothing_is_queued(env):
    """Negative, through real auth: a platform-owner token is not an admin token."""
    from src.db.models import PlatformOwner
    from src.modules.auth.tokens import platform_owner_token_data
    from src.utils.auth import create_platform_owner_access_token

    async with async_session_factory() as db:
        owner = PlatformOwner(email=f"owner-{uuid.uuid4().hex[:8]}@throwaway.test", password_hash=hash_password(PASSWORD),
                              name="Owner", is_active=True, must_complete_security_setup=False)
        db.add(owner)
        await db.commit()
    token = create_platform_owner_access_token(platform_owner_token_data(owner))
    env["queued"].clear()
    res = await env["client"].post("/payments/reconcile", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code in (401, 403)
    assert env["queued"] == []


async def test_scoped_run_touches_only_that_operator(env):
    a, b = await make_tenant(), await make_tenant()
    a_stale, a_check = await stale_and_checkable(a)
    b_stale, b_check = await stale_and_checkable(b)
    env["looked_up"].clear()

    stats = await run_payment_reconciliation(isp_operator_id=str(a["operator"].id))

    assert await status_of(a_stale) == "failed"
    assert a_check in env["looked_up"]
    # Negative: the other operator's payments are neither looked up at their
    # provider nor timed out.
    assert b_check not in env["looked_up"] and b_stale not in env["looked_up"]
    assert await status_of(b_stale) == "pending" and await status_of(b_check) == "pending"
    assert stats["processed"] == 2 and stats["timeout"] == 1 and stats["errors"] == 1


async def test_scheduled_run_still_covers_every_operator(env):
    a, b = await make_tenant(), await make_tenant()
    a_stale, _ = await stale_and_checkable(a)
    b_stale, b_check = await stale_and_checkable(b)
    env["looked_up"].clear()

    await run_payment_reconciliation()  # as the cron calls it: ctx only

    assert await status_of(a_stale) == "failed" and await status_of(b_stale) == "failed"
    assert b_check in env["looked_up"]


async def test_fresh_payments_are_left_alone_by_a_scoped_run(env):
    """Negative: scoping doesn't change the 15-minute floor."""
    a = await make_tenant()
    fresh = await make_pending(a, age=timedelta(minutes=2), provider_reference=f"PSK-{uuid.uuid4().hex[:10]}")
    env["looked_up"].clear()
    await run_payment_reconciliation(isp_operator_id=str(a["operator"].id))
    assert fresh not in env["looked_up"] and await status_of(fresh) == "pending"
