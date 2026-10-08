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
    from src.db.models import AdminUser, AuditLog, ISPOperator, Plan, Router, Voucher
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


# ── Batch 2: towns, sites, routers, setup, WireGuard, config templates ──────

NAS_SECRET = "Nas-Secret-9f3k2"
API_PASSWORD = "Api-Passw0rd-x7q"


@pytest.fixture
def fake_router_side(monkeypatch):
    """Everything that would reach a router or the VPN server, recorded."""
    from types import SimpleNamespace

    from src.modules.mikrotik import routes as mt_routes
    from src.modules.mikrotik import setup_routes
    from src.modules.mikrotik.api_service import MikroTikOperationError
    from src.modules.mikrotik.types import ConnectionTestResult
    from src.modules.wireguard import routes as wg_routes

    calls = []

    async def test_connection_async(**kw):
        calls.append(("test_connection", kw["password"]))
        return ConnectionTestResult(success=False, error=f"connection refused for admin/{kw['password']}")

    async def list_interfaces_temp(**kw):
        raise MikroTikOperationError(f"host unreachable (login {kw['password']})")

    async def disconnect_hotspot_user(router_id, active_id):
        return SimpleNamespace(model_dump=lambda: {"ok": True})

    async def get_active_hotspot_users(router_id):
        return []

    async def reboot_router(router_id):
        return SimpleNamespace(commands_executed=[])

    async def apply_radius(router_id, data):
        calls.append(("apply_radius", data["radius_secret"]))
        return "radius configured", []

    async def apply_network(router_id, data):
        raise MikroTikOperationError("bridge exists", commands=[])

    async def noop(*a, **k):
        return None

    async def peer_down(public_key, *a, **k):
        from src.modules.wireguard.service import WireGuardError
        raise WireGuardError(f"sidecar unreachable removing {public_key}")

    monkeypatch.setattr(mt_routes.service, "test_connection_async", test_connection_async)
    monkeypatch.setattr(mt_routes.service, "list_interfaces_temp", list_interfaces_temp)
    monkeypatch.setattr(mt_routes.service, "disconnect_hotspot_user", disconnect_hotspot_user)
    monkeypatch.setattr(mt_routes.service, "get_active_hotspot_users", get_active_hotspot_users)
    monkeypatch.setattr(mt_routes.service, "reboot_router", reboot_router)
    monkeypatch.setattr(mt_routes.provisioner, "launch_provision", lambda **kw: None)
    monkeypatch.setattr(mt_routes.provisioner, "launch_apply_template", lambda **kw: None)
    monkeypatch.setattr(mt_routes.wg_service, "remove_peer", noop)
    monkeypatch.setattr(setup_routes.setup, "apply_radius", apply_radius)
    monkeypatch.setattr(setup_routes.setup, "apply_network", apply_network)
    monkeypatch.setattr(wg_routes.wg, "add_peer", noop)
    monkeypatch.setattr(wg_routes.wg, "remove_peer", peer_down)
    monkeypatch.setattr(wg_routes.settings, "wg_server_public_key", "server-pub")
    return calls


async def make_site(env, t) -> dict:
    c = env["client"]
    town = (await c.post("/towns", headers=t["h"], json={"name": f"T-{uuid.uuid4().hex[:6]}", "region": "Volta"})).json()
    res = await c.post(f"/towns/{town['id']}/sites", headers=t["h"], json={"name": "Main", "address": "1 Road"})
    assert res.status_code == 201, res.text
    return {"town": town, "site": res.json()}


async def onboard(env, t, site_id) -> str:
    res = await env["client"].post("/admin/routers/onboard", headers=t["h"], json={
        "site_id": site_id, "name": f"R-{uuid.uuid4().hex[:6]}", "nas_identifier": f"nas-{uuid.uuid4().hex[:6]}",
        "nas_secret": NAS_SECRET, "ip_address": "192.0.2.10"})
    assert res.status_code == 200, res.text
    return res.json()["router_id"]


def assert_absent(row, *secrets):
    text = text_of(row) + json.dumps([row.target_id])
    for secret in secrets:
        assert secret not in text, f"{secret!r} leaked into {row.action}"


async def test_town_and_site_rows(env, tenant):
    made = await make_site(env, tenant)
    town_id = made["town"]["id"]
    c = env["client"]
    assert (await c.put(f"/towns/{town_id}", headers=tenant["h"], json={"region": "Oti"})).status_code == 200
    assert (await c.put(f"/towns/{town_id}", headers=tenant["h"], json={"region": "Oti"})).status_code == 200  # no-op
    create, update = await rows(tenant["operator"], target_id=town_id)
    assert (create.action, create.changes["region"]) == ("town.create", {"before": None, "after": "Volta"})
    assert (update.action, update.changes) == ("town.update", {"region": {"before": "Volta", "after": "Oti"}})
    [site] = await rows(tenant["operator"], "site.create", made["site"]["id"])
    assert site.changes["town_id"] == {"before": None, "after": town_id}
    assert_actor(site, tenant)


async def test_router_create_and_update_never_show_the_nas_secret(env, tenant):
    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    c = env["client"]
    res = await c.put(f"/sites/routers/{rid}", headers=tenant["h"], json={"name": "Renamed", "nas_secret": "Other-Secret-55"})
    assert res.status_code == 200, res.text
    other = (await c.post(f"/sites/{made['site']['id']}/routers", headers=tenant["h"], json={
        "name": "Second", "ip_address": "", "nas_identifier": f"nas-{uuid.uuid4().hex[:6]}", "nas_secret": NAS_SECRET})).json()
    create, update = await rows(tenant["operator"], target_id=rid)
    assert create.action == "router.create" and create.changes["nas_secret_plain"] == REDACTED
    assert create.changes["ip_address"] == {"before": None, "after": "192.0.2.10"}
    assert update.changes == {"name": {"before": create.changes["name"]["after"], "after": "Renamed"}, "nas_secret_plain": REDACTED}
    [second] = await rows(tenant["operator"], "router.create", other["id"])
    for row in (create, update, second):
        assert_actor(row, tenant)
        assert_absent(row, NAS_SECRET, "Other-Secret-55")


async def test_router_create_fails_closed(env, tenant, monkeypatch):
    made = await make_site(env, tenant)
    break_audit(monkeypatch)
    res = await env["client"].post("/admin/routers/onboard", headers=tenant["h"], json={
        "site_id": made["site"]["id"], "name": "Never", "nas_identifier": "nas-never", "nas_secret": NAS_SECRET})
    assert res.status_code == 500
    async with async_session_factory() as db:
        assert (await db.execute(select(Router).where(Router.site_id == uuid.UUID(made["site"]["id"])))).scalars().all() == []


async def test_credentials_rows_never_show_the_api_password(env, tenant):
    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    c = env["client"]
    body = {"api_username": "api", "api_password": API_PASSWORD, "api_port": 8728}
    assert (await c.post(f"/admin/routers/{rid}/credentials", headers=tenant["h"], json=body)).status_code == 201
    # Same password, new port: the password is not reported as changed.
    assert (await c.put(f"/admin/routers/{rid}/credentials", headers=tenant["h"], json={**body, "api_port": 8729})).status_code == 200
    # New password: now it is, as "[redacted]".
    assert (await c.put(f"/admin/routers/{rid}/credentials", headers=tenant["h"],
                        json={**body, "api_port": 8729, "api_password": "Api-Passw0rd-NEW"})).status_code == 200
    # The same values again: no row at all.
    assert (await c.put(f"/admin/routers/{rid}/credentials", headers=tenant["h"],
                        json={**body, "api_port": 8729, "api_password": "Api-Passw0rd-NEW"})).status_code == 200
    first, second, third = await rows(tenant["operator"], "router.credentials_set", rid)
    assert first.changes["api_username"] == {"before": None, "after": "api"} and first.changes["api_password_encrypted"] == REDACTED
    assert second.changes == {"api_port": {"before": 8728, "after": 8729}}
    assert third.changes == {"api_password_encrypted": REDACTED}
    for row in (first, second, third):
        assert_absent(row, API_PASSWORD, "Api-Passw0rd-NEW")


async def test_probe_rows_say_where_but_never_the_password(env, tenant, fake_router_side):
    c = env["client"]
    probe = {"host": "198.51.100.7", "port": 8728, "username": "admin", "password": API_PASSWORD}
    assert (await c.post("/admin/routers/test-connection", headers=tenant["h"], json=probe)).status_code == 200
    assert (await c.post("/admin/routers/temp-interfaces", headers=tenant["h"], json=probe)).status_code == 422
    test, listing = await rows(tenant["operator"], target_id="198.51.100.7")
    assert (test.action, test.detail["outcome"], test.detail["error"]) == ("router.test_connection", "failed", f"connection refused for admin/{REDACTED}")
    assert listing.detail["error"] == f"host unreachable (login {REDACTED})"
    assert (listing.action, listing.detail["outcome"]) == ("router.list_interfaces", "failed")
    for row in (test, listing):
        assert_actor(row, tenant)
        assert_absent(row, API_PASSWORD)
        assert row.detail["api_username"] == "admin" and row.detail["host"] == "198.51.100.7"


async def _with_credentials(env, t, rid):
    await env["client"].post(f"/admin/routers/{rid}/credentials", headers=t["h"],
                             json={"api_username": "api", "api_password": API_PASSWORD})


async def test_provision_reboot_apply_template_rows(env, tenant, fake_router_side):
    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    await _with_credentials(env, tenant, rid)
    c = env["client"]
    tpl = (await c.post("/admin/config-templates", headers=tenant["h"], json={
        "name": f"tpl-{uuid.uuid4().hex[:8]}", "template_data": {"version": 1, "commands": [{"path": "/ip/dns"}]}})).json()
    prov = (await c.post(f"/admin/routers/{rid}/provision", headers=tenant["h"], json={"dns_name": "wifi.local"})).json()
    reboot = (await c.post(f"/admin/routers/{rid}/reboot", headers=tenant["h"], json={"confirm": True})).json()
    applied = (await c.post(f"/admin/routers/{rid}/apply-template", headers=tenant["h"], json={"template_id": tpl["id"]})).json()
    p, r, a = [await rows(tenant["operator"], action, rid) for action in ("router.provision", "router.reboot", "router.apply_template")]
    assert [x.detail["log_id"] for x in (p[0], r[0], a[0])] == [prov["log_id"], reboot["log_id"], applied["log_id"]]
    assert a[0].detail["template_name"] == tpl["name"]
    unconfirmed = await c.post(f"/admin/routers/{rid}/reboot", headers=tenant["h"], json={})
    assert unconfirmed.status_code == 422 and len(await rows(tenant["operator"], "router.reboot", rid)) == 1


async def test_reboot_fails_closed_before_reaching_the_router(env, tenant, fake_router_side, monkeypatch):
    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    await _with_credentials(env, tenant, rid)
    break_audit(monkeypatch)
    res = await env["client"].post(f"/admin/routers/{rid}/reboot", headers=tenant["h"], json={"confirm": True})
    assert res.status_code == 500
    from src.db.models import RouterProvisionLog
    async with async_session_factory() as db:
        assert (await db.execute(select(RouterProvisionLog).where(RouterProvisionLog.router_id == uuid.UUID(rid)))).scalars().all() == []


async def test_disconnect_user_row_has_the_masked_voucher(env, tenant, fake_router_side):
    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    plan = await new_plan(env, tenant)
    voucher = (await env["client"].post("/vouchers/generate", headers=tenant["h"], json={"plan_id": plan["id"], "quantity": 1})).json()[0]
    res = await env["client"].post(f"/admin/routers/{rid}/disconnect-user", headers=tenant["h"],
                                   json={"active_id": "*1", "username": voucher["username"]})
    assert res.status_code == 200, res.text
    [row] = await rows(tenant["operator"], "router.disconnect_user", voucher["id"])
    suffix = "…" + voucher["code"].replace("-", "")[-4:]
    assert row.detail["code_suffix"] == suffix and suffix in row.summary and row.detail["outcome"] == "ok"
    assert_actor(row, tenant)
    assert_no_voucher_secrets(row, await vouchers_of(plan["id"]))


async def test_setup_rows_never_carry_the_radius_secret(env, tenant, fake_router_side):
    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    async with async_session_factory() as db:
        await db.execute(update(Router).where(Router.id == uuid.UUID(rid)).values(wg_enabled=True, wg_is_connected=True, wg_tunnel_ip="10.100.0.77"))
        await db.commit()
    c = env["client"]
    assert (await c.post(f"/admin/routers/{rid}/setup/radius/apply", headers=tenant["h"], json={})).status_code == 200
    assert (await c.post(f"/admin/routers/{rid}/setup/network/apply", headers=tenant["h"], json={"gateway_ip": "10.5.50.1"})).status_code == 422
    assert ("apply_radius", NAS_SECRET) in fake_router_side, "the router really got the secret"
    [radius] = await rows(tenant["operator"], "router.setup_radius", rid)
    [network] = await rows(tenant["operator"], "router.setup_network", rid)
    assert radius.detail["outcome"] == "ok" and radius.detail["config"]["radius_host"]
    assert (network.detail["outcome"], network.detail["error"]) == ("failed", "bridge exists")
    for row in (radius, network):
        assert_actor(row, tenant)
        assert_absent(row, NAS_SECRET)


async def test_wireguard_rows_never_carry_keys(env, tenant, fake_router_side):
    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    c = env["client"]
    setup = (await c.post(f"/admin/routers/{rid}/wireguard/setup", headers=tenant["h"])).json()
    again = await c.post(f"/admin/routers/{rid}/wireguard/setup", headers=tenant["h"])  # re-asserts the peer
    assert again.status_code == 200
    async with async_session_factory() as db:
        private = (await db.get(Router, uuid.UUID(rid))).wg_peer_private_key_encrypted
    assert (await c.delete(f"/admin/routers/{rid}/wireguard", headers=tenant["h"])).status_code == 200
    created, resync, removed = [
        (await rows(tenant["operator"], a, rid))[0] for a in ("router.wireguard_setup", "router.wireguard_peer", "router.wireguard_remove")]
    assert created.changes["wg_tunnel_ip"] == {"before": None, "after": setup["tunnel_ip"]}
    assert created.changes["wg_peer_public_key"] == REDACTED and created.changes["wg_peer_private_key_encrypted"] == REDACTED
    assert resync.detail == {"tunnel_ip": setup["tunnel_ip"], "reused_tunnel": True, "outcome": "ok"}
    assert removed.detail == {"peer_removed_on_server": False, "peer_error": f"sidecar unreachable removing {REDACTED}"}
    for row in (created, resync, removed):
        assert_actor(row, tenant)
        assert_absent(row, setup["router_public_key"], private)


async def test_router_removal_rows(env, tenant, fake_router_side):
    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    c = env["client"]
    assert (await c.delete(f"/admin/routers/{rid}", headers=tenant["h"])).status_code == 200
    assert (await c.delete(f"/admin/routers/{rid}", headers=tenant["h"])).status_code == 409
    removal = await rows(tenant["operator"], "router.remove", rid)
    result = await rows(tenant["operator"], "router.remove_result", rid)
    assert len(removal) == 1 and len(result) == 1
    assert removal[0].changes["is_active"] == {"before": True, "after": False}
    assert result[0].detail["sessions_disconnected"] == 0 and result[0].detail["wireguard_removed"] is True


async def test_config_template_rows_hide_the_template_body(env, tenant):
    c = env["client"]
    body = {"name": f"tpl-{uuid.uuid4().hex[:8]}", "template_data": {"version": 1, "commands": [{"path": "/user", "params": {"password": "Tpl-Pass-1"}}]}}
    tpl = (await c.post("/admin/config-templates", headers=tenant["h"], json=body)).json()
    assert (await c.put(f"/admin/config-templates/{tpl['id']}", headers=tenant["h"], json={"description": "v2",
            "template_data": {"version": 1, "commands": [{"path": "/ip/dns"}]}})).status_code == 200
    bad = await c.put(f"/admin/config-templates/{tpl['id']}", headers=tenant["h"], json={"template_data": {"version": 2}})
    assert bad.status_code == 422
    assert (await c.delete(f"/admin/config-templates/{tpl['id']}", headers=tenant["h"])).status_code == 204
    create, update_row, delete = await rows(tenant["operator"], target_id=tpl["id"])
    assert [r.action for r in (create, update_row, delete)] == ["config_template.create", "config_template.update", "config_template.delete"]
    assert create.changes["template_data"] == "changed" and update_row.changes["template_data"] == "changed"
    assert update_row.changes["description"] == {"before": None, "after": "v2"}
    for row in (create, update_row, delete):
        assert_absent(row, "Tpl-Pass-1", "/ip/dns")


async def test_rename_without_touching_the_secret_does_not_mention_it(env, tenant):
    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    c = env["client"]
    assert (await c.put(f"/sites/routers/{rid}", headers=tenant["h"], json={"name": "Renamed"})).status_code == 200
    # The same secret sent back (an edit form that always posts it) is not a change either.
    assert (await c.put(f"/sites/routers/{rid}", headers=tenant["h"], json={"name": "Renamed2", "nas_secret": NAS_SECRET})).status_code == 200
    _create, rename, rename_again = await rows(tenant["operator"], target_id=rid)
    assert rename.changes == {"name": {"before": _create.changes["name"]["after"], "after": "Renamed"}}
    assert rename_again.changes == {"name": {"before": "Renamed", "after": "Renamed2"}}
    for row in (rename, rename_again):
        assert "nas_secret_plain" not in row.changes and REDACTED not in text_of(row)


async def test_failed_radius_apply_never_stores_the_secret_it_echoed(env, tenant, fake_router_side, monkeypatch):
    """The router rejects the command and quotes it back, secret included."""
    from src.modules.mikrotik import setup_routes
    from src.modules.mikrotik.api_service import MikroTikOperationError

    async def rejected(router_id, data):
        # Long enough that the secret sits across the 500-character cap.
        raise MikroTikOperationError("x" * 490 + f" secret={data['radius_secret']} rejected", commands=[])

    monkeypatch.setattr(setup_routes.setup, "apply_radius", rejected)
    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    await _with_credentials(env, tenant, rid)
    async with async_session_factory() as db:
        await db.execute(update(Router).where(Router.id == uuid.UUID(rid)).values(wg_enabled=True, wg_is_connected=True, wg_tunnel_ip="10.100.0.78"))
        await db.commit()
    res = await env["client"].post(f"/admin/routers/{rid}/setup/radius/apply", headers=tenant["h"], json={})
    assert res.status_code == 422
    [row] = await rows(tenant["operator"], "router.setup_radius", rid)
    assert row.detail["outcome"] == "failed" and len(row.detail["error"]) <= 501
    assert_absent(row, NAS_SECRET, NAS_SECRET[:5], API_PASSWORD)

    # The same with a short message: the secret's place is marked, the rest kept.
    async def short(router_id, data):
        raise MikroTikOperationError(f"bad secret={data['radius_secret']}", commands=[])

    monkeypatch.setattr(setup_routes.setup, "apply_radius", short)
    assert (await env["client"].post(f"/admin/routers/{rid}/setup/radius/apply", headers=tenant["h"], json={})).status_code == 422
    _first, second = await rows(tenant["operator"], "router.setup_radius", rid)
    assert second.detail["error"] == f"bad secret={REDACTED}"


async def test_failed_disconnect_never_stores_the_api_password_it_echoed(env, tenant, fake_router_side, monkeypatch):
    from src.modules.mikrotik import routes as mt_routes
    from src.modules.mikrotik.api_service import MikroTikOperationError

    async def refused(router_id, active_id):
        raise MikroTikOperationError(f"login failure for api/{API_PASSWORD}")

    monkeypatch.setattr(mt_routes.service, "disconnect_hotspot_user", refused)
    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    await _with_credentials(env, tenant, rid)
    plan = await new_plan(env, tenant)
    voucher = (await env["client"].post("/vouchers/generate", headers=tenant["h"], json={"plan_id": plan["id"], "quantity": 1})).json()[0]
    res = await env["client"].post(f"/admin/routers/{rid}/disconnect-user", headers=tenant["h"],
                                   json={"active_id": "*1", "username": voucher["username"]})
    assert res.status_code >= 400
    [row] = await rows(tenant["operator"], "router.disconnect_user", voucher["id"])
    assert row.detail["outcome"] == "failed" and row.detail["error"] == f"login failure for api/{REDACTED}"
    assert_absent(row, API_PASSWORD)
