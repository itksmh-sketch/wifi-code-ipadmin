"""P4 audit instrumentation against a REAL database: the row each audited
action writes, that a refused or failed action writes none, that a failed
audit write fails the action (fail closed), and that no secret value reaches
``changes`` or ``detail``. One section per batch.

Runs the FastAPI app in-process. Only runs against a disposable database:

    AUDIT_INSTR_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>

(tests/throwaway_db.py run sets both.)
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("AUDIT_INSTR_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs AUDIT_INSTR_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import select, update

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import AdminUser, AuditLog, ISPOperator, Plan, Voucher
    from src.modules import audit
    from src.utils.auth import hash_password

PASSWORD = "Str0ngPassword"
REDACTED = "[redacted]"


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    async def no_limit(*args, **kwargs):
        return None

    # Every module that imported the limiter: the suites run without Redis.
    for name, module in list(sys.modules.items()):
        if name.startswith("src.") and hasattr(module, "enforce_rate_limit"):
            monkeypatch.setattr(module, "enforce_rate_limit", no_limit)

    # raise_app_exceptions=False: a fail-closed test expects a 500, not a raise.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as client:
        yield {"client": client}


async def make_operator():
    slug = f"aud-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Aud {slug}", slug=slug, contact_email=f"{slug}@throwaway.test", status="approved")
        db.add(operator)
        await db.commit()
        return operator


async def make_admin(operator, *, role="superadmin"):
    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        admin = AdminUser(
            isp_operator_id=operator.id, email=f"a-{uuid.uuid4().hex[:10]}@throwaway.test",
            password_hash=hash_password(PASSWORD), role=role, is_active=True, phone="233244000777",
            phone_verified=True, must_complete_onboarding=False, must_change_password=False,
            pin_hash=hash_password("482913"), pin_set_at=now, pin_verified_until=now + timedelta(minutes=10),
        )
        db.add(admin)
        await db.commit()
        return admin


async def sign_in(env, admin) -> dict:
    res = await env["client"].post("/auth/login", json={"email": admin.email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    # Sign-in ends any PIN elevation; put back the state the test created.
    async with async_session_factory() as db:
        await db.execute(update(AdminUser).where(AdminUser.id == admin.id).values(pin_verified_until=admin.pin_verified_until))
        await db.commit()
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest_asyncio.fixture(loop_scope="module")
async def tenant(env):
    operator = await make_operator()
    admin = await make_admin(operator)
    return {"operator": operator, "admin": admin, "h": await sign_in(env, admin)}


async def rows(operator, action=None, target_id=None):
    async with async_session_factory() as db:
        q = select(AuditLog).where(AuditLog.isp_operator_id == operator.id, ~AuditLog.action.startswith("auth."))
        if action:
            q = q.where(AuditLog.action == action)
        if target_id is not None:
            q = q.where(AuditLog.target_id == str(target_id))
        return (await db.execute(q.order_by(AuditLog.created_at))).scalars().all()


def assert_actor(row, t):
    assert (row.actor_type, row.actor_id, row.actor_label) == ("operator_user", t["admin"].id, t["admin"].email)
    assert row.isp_operator_id == t["operator"].id
    assert str(row.ip) == "127.0.0.1" and row.request_id and row.user_agent


def text_of(row) -> str:
    return json.dumps({"summary": row.summary, "changes": row.changes, "detail": row.detail}, default=str)


def plan_body(**extra):
    return {"name": f"P-{uuid.uuid4().hex[:6]}", "type": "time", "duration_minutes": 60,
            "price_ghs": 5.0, "download_speed_kbps": 2048, "upload_speed_kbps": 1024, **extra}


async def new_plan(env, t, **extra) -> dict:
    res = await env["client"].post("/plans", headers=t["h"], json=plan_body(**extra))
    assert res.status_code == 201, res.text
    return res.json()


def break_audit(monkeypatch):
    """Make every audit insert fail the way a broken table would."""
    def boom(*args, **kwargs):
        raise RuntimeError("audit insert failed")
    monkeypatch.setattr(audit, "record", boom)


# ── Batch 1: plans ──────────────────────────────────────────────────────────

async def test_plan_create_row(env, tenant):
    plan = await new_plan(env, tenant, price_ghs=7.5)
    [row] = await rows(tenant["operator"], "plan.create", plan["id"])
    assert_actor(row, tenant)
    assert row.target_type == "plan" and row.summary == f"Created plan {plan['name']}"
    assert row.changes["price_ghs"] == {"before": None, "after": "7.50"}
    assert row.changes["name"] == {"before": None, "after": plan["name"]}


async def test_plan_update_rows_show_before_and_after(env, tenant):
    plan = await new_plan(env, tenant)
    c = env["client"]
    assert (await c.patch(f"/plans/{plan['id']}", headers=tenant["h"], json={"name": "Renamed " + plan["name"]})).status_code == 200
    assert (await c.put(f"/plans/{plan['id']}", headers=tenant["h"], json={"download_speed_kbps": 4096})).status_code == 200
    assert (await c.patch(f"/plans/{plan['id']}", headers=tenant["h"], json={"price_ghs": 6})).status_code == 200
    first, second, third = await rows(tenant["operator"], "plan.update", plan["id"])
    assert first.changes == {"name": {"before": plan["name"], "after": "Renamed " + plan["name"]}}
    assert second.changes == {"download_speed_kbps": {"before": 2048, "after": 4096}}
    assert third.changes == {"price_ghs": {"before": "5.00", "after": "6.00"}}
    assert_actor(second, tenant)


async def test_a_no_op_edit_writes_no_row(env, tenant):
    plan = await new_plan(env, tenant)
    for same in ({"name": plan["name"]}, {"price_ghs": 5}):
        res = await env["client"].patch(f"/plans/{plan['id']}", headers=tenant["h"], json=same)
        assert res.status_code == 200
    assert await rows(tenant["operator"], "plan.update", plan["id"]) == []


async def test_a_refused_plan_writes_no_row(env, tenant):
    plan = await new_plan(env, tenant)
    before = len(await rows(tenant["operator"], "plan.create"))
    dup = plan_body(name="Dup " + plan["name"])  # same settings as an existing plan
    res = await env["client"].post("/plans", headers=tenant["h"], json=dup)
    assert res.status_code == 409
    entitlement = await env["client"].patch(f"/plans/{plan['id']}", headers=tenant["h"], json={"duration_minutes": 5})
    assert entitlement.status_code == 400
    assert len(await rows(tenant["operator"], "plan.create")) == before
    assert await rows(tenant["operator"], "plan.update", plan["id"]) == []


async def test_activate_deactivate_rows_and_no_op(env, tenant):
    plan = await new_plan(env, tenant)
    c = env["client"]
    assert (await c.post(f"/plans/{plan['id']}/deactivate", headers=tenant["h"])).status_code == 200
    assert (await c.post(f"/plans/{plan['id']}/deactivate", headers=tenant["h"])).status_code == 200  # already off
    assert (await c.post(f"/plans/{plan['id']}/activate", headers=tenant["h"])).status_code == 200
    actions = [(r.action, r.changes) for r in await rows(tenant["operator"], target_id=plan["id"])]
    assert actions[1:] == [
        ("plan.deactivate", {"is_active": {"before": True, "after": False}}),
        ("plan.activate", {"is_active": {"before": False, "after": True}}),
    ]


async def test_plan_delete_row(env, tenant):
    plan = await new_plan(env, tenant)
    await env["client"].post("/vouchers/generate", headers=tenant["h"], json={"plan_id": plan["id"], "quantity": 2})
    assert (await env["client"].delete(f"/plans/{plan['id']}", headers=tenant["h"])).status_code == 204
    [row] = await rows(tenant["operator"], "plan.delete", plan["id"])
    assert row.detail == {"vouchers_deleted": 2, "commission_rules_deleted": 0}
    assert row.changes["name"] == {"before": plan["name"], "after": None}


async def test_plan_write_fails_closed(env, tenant, monkeypatch):
    break_audit(monkeypatch)
    body = plan_body()
    res = await env["client"].post("/plans", headers=tenant["h"], json=body)
    assert res.status_code == 500
    async with async_session_factory() as db:
        assert (await db.execute(select(Plan).where(Plan.name == body["name"]))).scalar_one_or_none() is None


async def test_staff_refused_writes_no_row(env, tenant):
    staff = await make_admin(tenant["operator"], role="staff")
    before = len(await rows(tenant["operator"]))
    res = await env["client"].post("/plans", headers=await sign_in(env, staff), json=plan_body())
    assert res.status_code == 403
    assert len(await rows(tenant["operator"])) == before


# ── Batch 1: vouchers ───────────────────────────────────────────────────────

async def vouchers_of(plan_id):
    async with async_session_factory() as db:
        return (await db.execute(select(Voucher).where(Voucher.plan_id == plan_id))).scalars().all()


def assert_no_voucher_secrets(row, vouchers):
    """No voucher's full code (with or without dashes), username or password
    anywhere in the row: summary, changes, detail, target or actor fields."""
    text = text_of(row) + json.dumps([row.target_id, row.actor_label, row.user_agent])
    for v in vouchers:
        for secret in (v.code, v.code.replace("-", ""), v.username, v.password):
            assert secret not in text, f"{secret!r} leaked into {row.action}"


async def test_generate_row_one_per_batch(env, tenant):
    plan = await new_plan(env, tenant)
    res = await env["client"].post("/vouchers/generate", headers=tenant["h"],
                                   json={"plan_id": plan["id"], "quantity": 3, "code_length": 12})
    assert res.status_code == 201, res.text
    batch = res.json()[0]["batch_id"]
    [row] = await rows(tenant["operator"], "voucher.generate")
    assert_actor(row, tenant)
    assert (row.target_type, row.target_id) == ("voucher_batch", batch)
    assert row.detail == {"plan_id": plan["id"], "plan_name": plan["name"], "quantity": 3, "site_id": None,
                          "characters": 12, "device_policy": "single"}
    assert row.changes is None
    assert_no_voucher_secrets(row, await vouchers_of(plan["id"]))


async def test_generate_fails_closed(env, tenant, monkeypatch):
    plan = await new_plan(env, tenant)
    break_audit(monkeypatch)
    res = await env["client"].post("/vouchers/generate", headers=tenant["h"], json={"plan_id": plan["id"], "quantity": 2})
    assert res.status_code == 500
    assert await vouchers_of(plan["id"]) == []


async def test_print_confirm_row_and_reconfirm_writes_none(env, tenant):
    plan = await new_plan(env, tenant)
    made = (await env["client"].post("/vouchers/generate", headers=tenant["h"], json={"plan_id": plan["id"], "quantity": 3})).json()
    ids = [v["id"] for v in made]
    c = env["client"]
    first = await c.post("/vouchers/print-batch/confirm", headers=tenant["h"], json={"voucher_ids": ids[:2]})
    again = await c.post("/vouchers/print-batch/confirm", headers=tenant["h"], json={"voucher_ids": ids[:2]})
    assert first.json()["confirmed"] == 2 and again.json()["confirmed"] == 0
    [row] = await rows(tenant["operator"], "voucher.print_confirm")
    assert row.detail == {"requested": 2, "confirmed": 2}
    assert_actor(row, tenant)
    assert_no_voucher_secrets(row, await vouchers_of(plan["id"]))


async def test_disable_and_reactivate_rows(env, tenant):
    plan = await new_plan(env, tenant)
    made = (await env["client"].post("/vouchers/generate", headers=tenant["h"], json={"plan_id": plan["id"], "quantity": 1})).json()
    vid = made[0]["id"]
    c = env["client"]
    assert (await c.put(f"/vouchers/{vid}/disable", headers=tenant["h"])).status_code == 200
    assert (await c.put(f"/vouchers/{vid}/disable", headers=tenant["h"])).status_code == 400  # already disabled
    assert (await c.put(f"/vouchers/{vid}/reactivate", headers=tenant["h"])).status_code == 200
    disable, reactivate = await rows(tenant["operator"], target_id=vid)
    suffix = "…" + made[0]["code"].replace("-", "")[-4:]
    assert (disable.action, disable.changes) == ("voucher.disable", {"status": {"before": "unused", "after": "disabled"}})
    assert disable.summary == f"Disabled voucher {suffix}"
    assert disable.detail == {"code_suffix": suffix, "plan_id": plan["id"], "batch_id": made[0]["batch_id"], "had_live_session": False}
    assert (reactivate.action, reactivate.changes) == ("voucher.reactivate", {"status": {"before": "disabled", "after": "unused"}})
    assert reactivate.summary == f"Reactivated voucher {suffix} (unused)"
    assert reactivate.detail == {"code_suffix": suffix, "plan_id": plan["id"], "batch_id": made[0]["batch_id"]}
    for row in (disable, reactivate):
        assert_actor(row, tenant)
        assert_no_voucher_secrets(row, await vouchers_of(plan["id"]))
    async with async_session_factory() as db:
        assert (await db.get(Voucher, uuid.UUID(vid))).status == "unused"


async def test_disable_fails_closed(env, tenant, monkeypatch):
    plan = await new_plan(env, tenant)
    vid = (await env["client"].post("/vouchers/generate", headers=tenant["h"], json={"plan_id": plan["id"], "quantity": 1})).json()[0]["id"]
    break_audit(monkeypatch)
    assert (await env["client"].put(f"/vouchers/{vid}/disable", headers=tenant["h"])).status_code == 500
    async with async_session_factory() as db:
        assert (await db.get(Voucher, uuid.UUID(vid))).status == "unused"


async def test_another_operators_voucher_writes_no_row(env, tenant):
    other = await make_operator()
    other_admin = await make_admin(other)
    other_h = await sign_in(env, other_admin)
    plan = (await env["client"].post("/plans", headers=other_h, json=plan_body())).json()
    vid = (await env["client"].post("/vouchers/generate", headers=other_h, json={"plan_id": plan["id"], "quantity": 1})).json()[0]["id"]
    before = len(await rows(tenant["operator"]))
    assert (await env["client"].put(f"/vouchers/{vid}/disable", headers=tenant["h"])).status_code == 404
    assert len(await rows(tenant["operator"])) == before
    assert await rows(other, "voucher.disable") == []
