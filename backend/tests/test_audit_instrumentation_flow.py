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


# ── Batch 3: payments, payment and SMS credentials, invoice pay, NAS secret ─

PAYSTACK = {"public_key": "pk_live_PUB0a1b2c3d4e5f6", "secret_key": "sk_live_SEC9z8y7x6w5v4u3t",
            "webhook_secret": "whsec_HOOK1q2w3e4r5t6y"}
FLUTTERWAVE = {"public_key": "FLWPUBK-pub112233445566-X", "secret_key": "FLWSECK-sec665544332211-X",
               "webhook_secret": "flw-hook-778899aabbcc"}
ARKESEL = {"api_key": "ArkKey-5f4e3d2c1b0a9988", "from": "WIFIZONE"}
PLATFORM_KEYS = {"secret_key": "sk_live_PLATFORMs3cr3t000111", "public_key": "pk_live_PLATFORMpub000111",
                 "webhook_secret": "plat-hook-445566778899"}
PHONE = "233241234567"
PHONE_FORMS = (PHONE, "0241234567", "241234567")
ALL_CREDENTIAL_VALUES = (*PAYSTACK.values(), *FLUTTERWAVE.values(), ARKESEL["api_key"], *PLATFORM_KEYS.values())


def assert_clean(row, *values):
    """No value anywhere in the row: summary, changes, detail, target, actor, user agent."""
    text = json.dumps({"summary": row.summary, "changes": row.changes, "detail": row.detail,
                       "target_type": row.target_type, "target_id": row.target_id,
                       "actor_label": row.actor_label, "user_agent": row.user_agent}, default=str)
    for value in (*values, *ALL_CREDENTIAL_VALUES, *PHONE_FORMS, NAS_SECRET):
        assert value not in text, f"{value!r} leaked into {row.action}"
    for fragment in ("sk_live_", "FLWSECK", "pk_live_P"):
        assert fragment not in text, f"{fragment!r} leaked into {row.action}"


@pytest_asyncio.fixture(loop_scope="module")
async def providers_offered():
    """The template offers only Paystack; this suite's clone offers the rest."""
    from src.db.models import ProviderCatalogEntry

    async with async_session_factory() as db:
        await db.execute(update(ProviderCatalogEntry).where(ProviderCatalogEntry.provider_key.in_(
            ["flutterwave", "arkesel", "arkesel_platform"])).values(is_available=True))
        await db.commit()


async def put_creds(env, t, kind, provider, values, **extra):
    return await env["client"].put(f"/{kind}-credentials/{provider}", headers=t["h"], json={"values": values, **extra})


async def test_payment_credentials_rows(env, tenant, providers_offered):
    c, h = env["client"], tenant["h"]
    assert (await put_creds(env, tenant, "payment", "paystack", PAYSTACK)).status_code == 200
    # The same values again: not a change, no row.
    assert (await put_creds(env, tenant, "payment", "paystack", PAYSTACK)).status_code == 200
    # Only the webhook secret differs: only it is listed.
    assert (await put_creds(env, tenant, "payment", "paystack", {**PAYSTACK, "webhook_secret": "whsec_NEW0987654321"})).status_code == 200
    assert (await put_creds(env, tenant, "payment", "flutterwave", FLUTTERWAVE, activate=True)).status_code == 200
    assert (await c.post("/payment-credentials/paystack/activate", headers=h)).status_code == 200
    assert (await c.post("/payment-credentials/paystack/activate", headers=h)).status_code == 200  # already active
    assert (await c.delete("/payment-credentials/flutterwave", headers=h)).status_code == 200

    added, webhook = await rows(tenant["operator"], "payment_credentials.set", "paystack")
    assert added.changes == {"public_key": REDACTED, "secret_key": REDACTED, "webhook_secret": REDACTED,
                             "is_active": {"before": None, "after": True}}
    assert added.detail == {"provider": "paystack", "created": True, "is_active": True, "deactivated": []}
    assert webhook.changes == {"webhook_secret": REDACTED}
    [flw] = await rows(tenant["operator"], "payment_credentials.set", "flutterwave")
    assert flw.detail["deactivated"] == ["paystack"] and flw.changes["is_active"] == {"before": None, "after": True}
    [activate] = await rows(tenant["operator"], "payment_credentials.activate", "paystack")
    assert activate.changes == {"is_active": {"before": False, "after": True}}
    assert activate.detail["deactivated"] == ["flutterwave"]
    [deleted] = await rows(tenant["operator"], "payment_credentials.delete", "flutterwave")
    assert deleted.changes == {"public_key": REDACTED, "secret_key": REDACTED, "webhook_secret": REDACTED}
    for row in (added, webhook, flw, activate, deleted):
        assert_actor(row, tenant)
        assert_clean(row, "whsec_NEW0987654321")


async def test_credentials_test_scrubs_a_key_the_provider_echoes(env, tenant, providers_offered, monkeypatch):
    from types import SimpleNamespace

    from src.modules.payments import credentials_routes

    key = PAYSTACK["secret_key"]

    async def rejected():
        # Paystack-style: the start of the key, then the whole key further on.
        raise RuntimeError(f"Invalid key {key[:14]}... (sent {key})")

    async def accepted():
        return None

    fake = SimpleNamespace(verify_credentials=rejected)
    monkeypatch.setattr(credentials_routes, "build_payment_provider", lambda *a, **k: fake)
    assert (await put_creds(env, tenant, "payment", "paystack", PAYSTACK)).status_code == 200
    res = await env["client"].post("/payment-credentials/paystack/test", headers=tenant["h"])
    assert res.status_code == 400
    assert key[:12] not in res.text, "the response is scrubbed too"
    fake.verify_credentials = accepted
    assert (await env["client"].post("/payment-credentials/paystack/test", headers=tenant["h"])).status_code == 200

    failed, ok = await rows(tenant["operator"], "payment_credentials.test", "paystack")
    assert failed.detail == {"provider": "paystack", "outcome": "failed", "error": f"Invalid key {REDACTED}... (sent {REDACTED})"}
    assert ok.detail == {"provider": "paystack", "outcome": "ok"}
    from src.db.models import OperatorPaymentCredential
    async with async_session_factory() as db:
        stored = (await db.execute(select(OperatorPaymentCredential).where(
            OperatorPaymentCredential.isp_operator_id == tenant["operator"].id))).scalar_one()
    assert stored.last_validation_error is None  # cleared by the passing test
    for row in (failed, ok):
        assert_actor(row, tenant)
        assert_clean(row)


async def test_sms_credentials_rows(env, tenant, providers_offered, monkeypatch):
    from src.modules.sms.providers.arkesel import ArkeselSMSProvider

    async def rejected(self):
        raise RuntimeError(f"Arkesel: invalid api key {ARKESEL['api_key']}")

    monkeypatch.setattr(ArkeselSMSProvider, "verify_credentials", rejected)
    c, h = env["client"], tenant["h"]
    assert (await put_creds(env, tenant, "sms", "arkesel", ARKESEL)).status_code == 200
    assert (await put_creds(env, tenant, "sms", "arkesel", {**ARKESEL, "from": "NEWNAME"})).status_code == 200
    assert (await c.post("/sms-credentials/arkesel/test", headers=h)).status_code == 400
    assert (await c.post("/sms-credentials/activate-platform", headers=h)).status_code == 200
    assert (await c.post("/sms-credentials/arkesel/activate", headers=h)).status_code == 200
    assert (await c.post("/sms-credentials/activate-platform", headers=h)).status_code == 200
    assert (await c.delete("/sms-credentials/arkesel", headers=h)).status_code == 200

    added, renamed = await rows(tenant["operator"], "sms_credentials.set", "arkesel")
    assert added.changes == {"api_key": REDACTED, "from": {"before": None, "after": "WIFIZONE"}, "is_active": {"before": None, "after": True}}
    assert renamed.changes == {"from": {"before": "WIFIZONE", "after": "NEWNAME"}}, "the unchanged key is not listed"
    [test] = await rows(tenant["operator"], "sms_credentials.test", "arkesel")
    assert test.detail["outcome"] == "failed" and test.detail["error"] == f"Arkesel: invalid api key {REDACTED}"
    platform_first, platform_again = await rows(tenant["operator"], "sms_credentials.activate", "arkesel_platform")
    assert platform_first.detail["deactivated"] == ["arkesel"]
    assert platform_again.detail["deactivated"] == ["arkesel"]
    [back] = await rows(tenant["operator"], "sms_credentials.activate", "arkesel")
    assert back.detail["deactivated"] == ["arkesel_platform"]
    [deleted] = await rows(tenant["operator"], "sms_credentials.delete", "arkesel")
    assert deleted.changes == {"api_key": REDACTED, "from": {"before": "NEWNAME", "after": None}}
    for row in (added, renamed, test, platform_first, platform_again, back, deleted):
        assert_actor(row, tenant)
        assert_clean(row)


async def make_payment(env, t, *, status="success") -> str:
    from decimal import Decimal

    from src.db.models import PaymentTransaction

    made = await make_site(env, t)
    # Plans with identical settings are refused as duplicates: vary the price.
    plan = await new_plan(env, t, price_ghs=float(1 + uuid.uuid4().int % 900))
    async with async_session_factory() as db:
        tx = PaymentTransaction(
            isp_operator_id=t["operator"].id, plan_id=uuid.UUID(plan["id"]), site_id=uuid.UUID(made["site"]["id"]),
            amount_ghs=Decimal("12.50"), payment_method="mtn_momo", provider="paystack", status=status,
            provider_reference=f"PSK-{uuid.uuid4().hex[:10]}", internal_reference=f"INT-{uuid.uuid4().hex[:12]}",
            phone_number=PHONE,
        )
        db.add(tx)
        await db.commit()
        return str(tx.id)


async def test_refund_row(env, tenant):
    pid = await make_payment(env, tenant)
    res = await env["client"].put(f"/payments/{pid}/refund", headers=tenant["h"])
    assert res.status_code == 200 and PHONE in res.text  # the API still returns it to the operator
    [row] = await rows(tenant["operator"], "payment.refund", pid)
    assert row.changes == {"status": {"before": "success", "after": "refunded"}}
    assert row.detail["amount_ghs"] == "12.50" and row.detail["provider_reference"].startswith("PSK-")
    assert (row.detail["outcome"], row.detail["provider_called"]) == ("refunded", False)
    assert_actor(row, tenant)
    assert_clean(row)


async def test_verify_rows_and_a_scrubbed_provider_error(env, tenant, providers_offered, monkeypatch):
    from types import SimpleNamespace

    from src.modules.payments.service import PaymentService

    assert (await put_creds(env, tenant, "payment", "paystack", PAYSTACK)).status_code == 200
    pid = await make_payment(env, tenant, status="pending")

    async def unreachable(self, db, tx):
        async def verify(*a, **k):
            raise RuntimeError(f"401 from Paystack: Invalid key {PAYSTACK['secret_key']}")
        return SimpleNamespace(verify=verify)

    monkeypatch.setattr(PaymentService, "provider_for_transaction", unreachable)
    assert (await env["client"].put(f"/payments/{pid}/verify", headers=tenant["h"])).status_code == 500

    async def settled(self, db, *, tx, force=False):
        tx.status = "success"
        await db.commit()
        return tx

    monkeypatch.setattr(PaymentService, "refresh_transaction_status", settled)
    res = await env["client"].put(f"/payments/{pid}/verify", headers=tenant["h"])
    assert res.status_code == 200, res.text

    failed, ok = await rows(tenant["operator"], "payment.verify", pid)
    assert (failed.detail["outcome"], failed.detail["status_before"], failed.detail["status_after"]) == ("failed", "pending", "pending")
    assert failed.detail["error"] == f"401 from Paystack: Invalid key {REDACTED}"
    assert (ok.detail["outcome"], ok.detail["status_before"], ok.detail["status_after"]) == ("ok", "pending", "success")
    assert ok.detail["amount_ghs"] == "12.50" and ok.detail["provider_reference"].startswith("PSK-")
    for row in (failed, ok):
        assert_actor(row, tenant)
        assert_clean(row)


async def test_export_row_has_filters_and_count_never_data(env, tenant, monkeypatch):
    await make_payment(env, tenant)
    await make_payment(env, tenant, status="failed")
    res = await env["client"].get("/payments/export", headers=tenant["h"], params={"status": "success"})
    assert res.status_code == 200 and PHONE in res.text, "the file itself has the data"
    [row] = await rows(tenant["operator"], "payment.export")
    assert row.detail["row_count"] == 1 and row.detail["filters"]["status"] == "success"
    assert row.detail["format"] == "csv" and row.summary == "Exported 1 payment(s) as CSV"
    assert_actor(row, tenant)
    assert_clean(row)
    # Fail closed: no row, no file.
    break_audit(monkeypatch)
    res = await env["client"].get("/payments/export", headers=tenant["h"])
    assert res.status_code == 500 and PHONE not in res.text
    assert len(await rows(tenant["operator"], "payment.export")) == 1


async def test_reconcile_row(env, tenant, monkeypatch):
    from types import SimpleNamespace

    from src.modules.payments import routes as payment_routes

    async def pool():
        async def enqueue_job(name, **kwargs):
            return SimpleNamespace(job_id="job-recon-1")
        return SimpleNamespace(enqueue_job=enqueue_job)

    monkeypatch.setattr(payment_routes, "get_redis_pool", pool)
    assert (await env["client"].post("/payments/reconcile", headers=tenant["h"])).status_code == 200
    [row] = await rows(tenant["operator"], "payment.reconcile")
    assert row.detail == {"queued": True, "job_id": "job-recon-1"} and row.target_id == "job-recon-1"
    assert_actor(row, tenant)
    assert_clean(row)


async def test_nas_secret_view_row_never_has_the_secret_or_hint(env, tenant, monkeypatch):
    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    res = await env["client"].get(f"/admin/routers/{rid}/nas-secret", headers=tenant["h"])
    assert res.status_code == 200
    hint = res.json()["hint"]
    [row] = await rows(tenant["operator"], "router.nas_secret_view", rid)
    assert row.target_type == "router" and row.changes is None and row.detail is None
    assert_actor(row, tenant)
    assert_clean(row, hint, NAS_SECRET[-3:])
    break_audit(monkeypatch)
    res = await env["client"].get(f"/admin/routers/{rid}/nas-secret", headers=tenant["h"])
    assert res.status_code == 500 and "hint" not in res.text


async def test_invoice_pay_rows(env, tenant, monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace

    from src.db.models import OperatorInvoice
    from src.modules.billing import service as billing_service
    from src.modules.platform import payment_credentials_service

    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        invoice = OperatorInvoice(isp_operator_id=tenant["operator"].id, invoice_number=f"INV-T-{uuid.uuid4().hex[:6]}",
                                  period_start=now - timedelta(days=30), period_end=now, amount_ghs=Decimal("50.00"),
                                  status="issued", issued_at=now)
        db.add(invoice)
        await db.commit()
        iid = str(invoice.id)

    async def keys(db):
        return SimpleNamespace(**PLATFORM_KEYS, is_configured=True)

    async def rejected(db, inv, operator):
        raise ValueError(f"Paystack error: Invalid key {PLATFORM_KEYS['secret_key'][:16]}")

    async def opened(db, inv, operator):
        return "https://checkout.paystack.com/secret-session-link"

    monkeypatch.setattr(payment_credentials_service, "resolve_paystack_keys", keys)
    monkeypatch.setattr(billing_service, "initiate_invoice_payment", rejected)
    res = await env["client"].post(f"/billing/invoices/{iid}/pay", headers=tenant["h"])
    assert res.status_code == 400 and "sk_live_PLAT" not in res.text and "PLATFORM" not in res.text
    monkeypatch.setattr(billing_service, "initiate_invoice_payment", opened)
    res = await env["client"].post(f"/billing/invoices/{iid}/pay", headers=tenant["h"])
    assert res.status_code == 200, res.text

    failed, ok = await rows(tenant["operator"], "billing.invoice_pay", iid)
    assert failed.detail["outcome"] == "failed" and failed.detail["error"] == f"Paystack error: Invalid key {REDACTED}"
    assert ok.detail["outcome"] == "checkout_opened" and ok.detail["amount_ghs"] == "50.00"
    assert ok.detail["reference"] == f"INV-{iid}"
    for row in (failed, ok):
        assert_actor(row, tenant)
        assert_clean(row, "secret-session-link")


# ── Stored error text outside audit_log: provision logs, setup status, CoA,
#    payment failure reasons, platform provider errors ─────────────────────

async def _tunnelled(rid):
    async with async_session_factory() as db:
        await db.execute(update(Router).where(Router.id == uuid.UUID(rid)).values(
            wg_enabled=True, wg_is_connected=True, wg_tunnel_ip=f"10.100.0.{50 + uuid.uuid4().int % 150}"))
        await db.commit()


async def test_provision_failure_stores_no_nas_secret(env, tenant, fake_router_side, monkeypatch):
    """The router rejects the RADIUS command and quotes it back; a config
    template put the secret under an innocent param name. Staff can read
    provision logs, so neither may keep it."""
    from src.db.models import RouterProvisionLog
    from src.modules.mikrotik import routes as mt_routes
    from src.modules.mikrotik.api_service import MikroTikOperationError

    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    await _with_credentials(env, tenant, rid)
    log_id = (await env["client"].post(f"/admin/routers/{rid}/provision", headers=tenant["h"], json={"dns_name": "wifi.local"})).json()["log_id"]

    async def rejected(router_id, *, radius_host, radius_secret):
        raise MikroTikOperationError(
            f"failure: /radius add address={radius_host} secret={radius_secret} rejected",
            commands=[{"path": "/radius/add", "params": {"comment": f"nas {radius_secret}", "secret": "***"}}],
        )

    async def host(router_id):
        return "10.100.0.1"

    provisioner = mt_routes.provisioner
    monkeypatch.setattr(provisioner.api_service, "set_radius_server", rejected)
    monkeypatch.setattr(provisioner, "_radius_host_for", host)
    await provisioner._run_provision(router_id=rid, log_id=log_id, dns_name="wifi.local", hotspot_interface=None, template_id=None)

    async with async_session_factory() as db:
        log = await db.get(RouterProvisionLog, uuid.UUID(log_id))
    assert log.status == "failed"
    assert log.error_message == f"failure: /radius add address=10.100.0.1 secret={REDACTED} rejected"
    stored = json.dumps(log.commands_executed)
    assert NAS_SECRET not in stored and f"nas {REDACTED}" in stored
    # What a staff user reads back.
    staff = await make_admin(tenant["operator"], role="staff")
    listing = await env["client"].get(f"/admin/routers/{rid}/provision-logs", headers=await sign_in(env, staff))
    assert listing.status_code == 200 and log_id in listing.text
    assert NAS_SECRET not in listing.text and API_PASSWORD not in listing.text


async def test_setup_apply_failure_stores_no_secret_in_log_or_status(env, tenant, fake_router_side, monkeypatch):
    from src.db.models import RouterProvisionLog, RouterSetupStatus
    from src.modules.mikrotik import setup_routes
    from src.modules.mikrotik.api_service import MikroTikOperationError

    async def rejected(router_id, data):
        raise MikroTikOperationError(f"bad secret={data['radius_secret']}",
                                     commands=[{"path": "/radius/add", "params": {"comment": data["radius_secret"]}}])

    monkeypatch.setattr(setup_routes.setup, "apply_radius", rejected)
    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    await _tunnelled(rid)
    assert (await env["client"].post(f"/admin/routers/{rid}/setup/radius/apply", headers=tenant["h"], json={})).status_code == 422
    async with async_session_factory() as db:
        log = (await db.execute(select(RouterProvisionLog).where(RouterProvisionLog.router_id == uuid.UUID(rid)))).scalar_one()
        status = (await db.execute(select(RouterSetupStatus).where(RouterSetupStatus.router_id == uuid.UUID(rid)))).scalar_one()
    assert log.error_message == f"bad secret={REDACTED}"
    assert NAS_SECRET not in json.dumps(log.commands_executed)
    assert status.radius_config == {"error": f"bad secret={REDACTED}"}


async def test_coa_failure_stores_no_nas_secret(env, tenant, monkeypatch):
    from types import SimpleNamespace

    from src.radius import coa_events

    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    await _tunnelled(rid)

    def refused(*, router_ip, router_secret, attributes):
        raise RuntimeError(f"NAK from {router_ip}: shared secret {router_secret} mismatch")

    async def no_cookies(*a, **k):
        return 0

    monkeypatch.setattr(coa_events, "send_disconnect_request", refused)
    monkeypatch.setattr(coa_events, "clear_router_cookies", no_cookies)
    monkeypatch.setattr(coa_events, "voucher_login_names", lambda *a, **k: [])
    event = SimpleNamespace(status=None, error_message=None, attempt_count=0, last_attempted_at=None)
    session = SimpleNamespace(ip_address="10.5.50.20", username="u1", mac_address=None)
    async with async_session_factory() as db:
        router = await db.get(Router, uuid.UUID(rid))
        result = await coa_events.send_disconnect_with_event(db, event=event, router=router, voucher=None, session=session)
    assert event.status == "failed" and NAS_SECRET not in event.error_message
    assert event.error_message.endswith(f"shared secret {REDACTED} mismatch")
    assert NAS_SECRET not in result["message"]


async def test_payment_failure_reason_stores_no_key(env, tenant, providers_offered):
    from src.db.models import PaymentTransaction
    from src.modules.payments.dependencies import get_payment_service

    assert (await put_creds(env, tenant, "payment", "paystack", PAYSTACK)).status_code == 200
    pid = await make_payment(env, tenant, status="pending")
    async with async_session_factory() as db:
        tx = await db.get(PaymentTransaction, uuid.UUID(pid))
        await get_payment_service()._resolve_failed_initiation(
            db, tx=tx, reason=f"Invalid key {PAYSTACK['secret_key']} (prefix {PAYSTACK['secret_key'][:12]})")
    async with async_session_factory() as db:
        tx = await db.get(PaymentTransaction, uuid.UUID(pid))
    assert tx.status == "failed"
    for column in (tx.failure_reason, tx.display_message):
        assert column == f"Invalid key {REDACTED} (prefix {REDACTED})"


async def test_platform_provider_error_stores_no_platform_key(env):
    from src.db.models import PlatformNotificationSMSCredential
    from src.modules.audit import stored_text
    from src.modules.credentials.service import dump_credentials

    key = "PlatNotifyKey-a1b2c3d4e5f6"
    async with async_session_factory() as db:
        db.add(PlatformNotificationSMSCredential(provider="arkesel", credentials_encrypted=dump_credentials(
            {"api_key": key, "sender_id": "IPADMIN"}), is_active=False))
        await db.commit()
        out = await stored_text.platform_error(db, f"Arkesel 401: api key {key} invalid ({key[:10]}…)")
    assert out == f"Arkesel 401: api key {REDACTED} invalid ({REDACTED}…)"


# ── Batch 4: resellers, commission, branding, SMS template, accounts ────────

ADMIN_PHONE_FORMS = ("233244000777", "0244000777", "244000777")
RESELLER_PASSWORD = "ResellerPass-77x"
RESELLER_PHONE_FORMS = ("233201112223", "0201112223", "201112223")


def assert_none_of(row, *values):
    text = json.dumps({"summary": row.summary, "changes": row.changes, "detail": row.detail,
                       "target_type": row.target_type, "target_id": row.target_id,
                       "actor_label": row.actor_label, "user_agent": row.user_agent}, default=str)
    for value in values:
        assert value and value not in text, f"{value!r} leaked into {row.action}"


async def all_rows(operator, action):
    """rows() leaves out auth.*; these tests want them."""
    async with async_session_factory() as db:
        q = select(AuditLog).where(AuditLog.isp_operator_id == operator.id, AuditLog.action == action)
        return (await db.execute(q.order_by(AuditLog.created_at))).scalars().all()


async def test_reseller_rows(env, tenant):
    c, h = env["client"], tenant["h"]
    email = f"res-{uuid.uuid4().hex[:8]}@throwaway.test"
    rid = (await c.post("/admin/resellers", headers=h, json={"name": "Kofi", "email": email, "phone": "0201112223",
                                                            "password": RESELLER_PASSWORD})).json()["id"]
    assert (await c.put(f"/admin/resellers/{rid}", headers=h, json={"name": "Kofi A"})).status_code == 200
    assert (await c.put(f"/admin/resellers/{rid}", headers=h, json={"name": "Kofi A"})).status_code == 200  # no-op
    assert (await c.put(f"/admin/resellers/{rid}", headers=h, json={"password": "NewResPass-88y", "phone": "0201112224"})).status_code == 200
    assert (await c.put(f"/admin/resellers/{rid}", headers=h, json={"commission_type": "fixed", "commission_value": "0.50"})).status_code == 200

    [created] = await rows(tenant["operator"], "reseller.create", rid)
    assert created.changes["email"] == {"before": None, "after": email} and created.changes["password_hash"] == REDACTED
    assert created.changes["phone"] == "changed"
    renamed, secured, commission = await rows(tenant["operator"], "reseller.update", rid)
    assert renamed.changes == {"name": {"before": "Kofi", "after": "Kofi A"}}, "no password, no phone: not touched"
    assert secured.changes == {"password_hash": REDACTED, "phone": "changed"}
    assert secured.detail == {"sessions_ended": True}
    assert commission.changes == {"commission_type": {"before": None, "after": "fixed"},
                                  "commission_value": {"before": None, "after": "0.50"}}
    for row in (created, renamed, secured, commission):
        assert_actor(row, tenant)
        assert_none_of(row, RESELLER_PASSWORD, "NewResPass-88y", *RESELLER_PHONE_FORMS, "0201112224")


async def test_reseller_defaults_rows(env, tenant):
    c, h = env["client"], tenant["h"]
    body = {"commission_type": "percent", "commission_value": 7.5, "redeem_days": 30}
    assert (await c.put("/admin/reseller-settings", headers=h, json=body)).status_code == 200
    assert (await c.put("/admin/reseller-settings", headers=h, json=body)).status_code == 200  # same values: no row
    [row] = await rows(tenant["operator"], "reseller.defaults_update", tenant["operator"].id)
    assert row.changes == {"reseller_commission_value": {"before": "0.00", "after": "7.50"},
                           "reseller_redeem_days": {"before": None, "after": 30}}
    assert_actor(row, tenant)


async def test_branding_rows_and_logo_fails_closed(env, tenant, monkeypatch, tmp_path):
    from src.modules.branding import routes as branding_routes

    monkeypatch.setattr(branding_routes, "_LOGO_DIR", str(tmp_path))
    c, h = env["client"], tenant["h"]
    body = {"primary_color": "#112233", "portal_contact_phone": "0244555666", "portal_welcome_message": "Karibu"}
    assert (await c.put("/admin/branding", headers=h, json=body)).status_code == 200
    assert (await c.put("/admin/branding", headers=h, json=body)).status_code == 200  # no-op
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    res = await c.post("/admin/branding/logo", headers=h, files={"file": ("l.png", png, "image/png")})
    assert res.status_code == 200, res.text
    [edit] = await rows(tenant["operator"], "branding.update")
    assert edit.changes["primary_color"]["after"] == "#112233" and edit.changes["portal_contact_phone"] == "changed"
    assert edit.changes["portal_welcome_message"] == {"before": None, "after": "Karibu"}
    [logo] = await rows(tenant["operator"], "branding.logo_upload")
    assert logo.changes["logo_url"]["after"] == res.json()["logo_url"] and logo.detail == {"content_type": "image/png", "bytes": len(png)}
    for row in (edit, logo):
        assert_actor(row, tenant)
        assert_none_of(row, "0244555666", "244555666")
    # Fail closed: no row, and the current logo is neither replaced nor deleted.
    current = os.listdir(tmp_path)
    break_audit(monkeypatch)
    assert (await c.post("/admin/branding/logo", headers=h, files={"file": ("m.png", png, "image/png")})).status_code == 500
    assert os.listdir(tmp_path) == current
    assert len(await rows(tenant["operator"], "branding.logo_upload")) == 1


async def test_sms_template_rows(env, tenant):
    c, h = env["client"], tenant["h"]
    assert (await c.put("/sms-template/voucher", headers=h, json={"template": "Your WiFi code: {code}"})).status_code == 200
    assert (await c.put("/sms-template/voucher", headers=h, json={"template": "Your WiFi code: {code}"})).status_code == 200
    assert (await c.put("/sms-template/voucher", headers=h, json={"template": None})).status_code == 200
    edited, reset = await rows(tenant["operator"], "sms_template.update")
    assert edited.changes == {"voucher_sms_template": {"before": None, "after": "Your WiFi code: {code}"}}
    assert reset.changes == {"voucher_sms_template": {"before": "Your WiFi code: {code}", "after": None}}
    assert reset.summary == "Reset the voucher SMS template to the default"
    for row in (edited, reset):
        assert_actor(row, tenant)


@pytest.fixture
def sent_codes(monkeypatch):
    """Every OTP the account routes would text, captured instead of sent."""
    from types import SimpleNamespace

    from src.modules.admin_accounts import routes as account_routes

    codes = []

    async def send(phone, code, purpose=None, **kw):
        codes.append(code)
        return SimpleNamespace(success=True, error=None)

    monkeypatch.setattr(account_routes, "send_otp_sms", send)
    return codes


async def test_signed_in_account_rows(env, tenant, sent_codes):
    c, admin = env["client"], tenant["admin"]
    assert (await c.post("/auth/me/pin/forgot", headers=tenant["h"])).status_code == 200
    assert (await c.post("/auth/me/phone", headers=tenant["h"], json={"phone": "0244000778"})).status_code == 200
    wrong = await c.post("/auth/me/password", headers=tenant["h"], json={
        "current_password": "Not-The-Password-1", "new_password": "Brand-New-Pass-42", "confirm_password": "Brand-New-Pass-42"})
    assert wrong.status_code == 400
    ok = await c.post("/auth/me/password", headers=tenant["h"], json={
        "current_password": PASSWORD, "new_password": "Brand-New-Pass-42", "confirm_password": "Brand-New-Pass-42"})
    assert ok.status_code == 200, ok.text

    [pin] = await rows(tenant["operator"], "account.pin_reset_code_sent")
    assert pin.detail == {"phone": "+233 •••• ••• 777", "outcome": "sent"}
    [phone] = await rows(tenant["operator"], "account.phone_change_code_sent")
    assert phone.detail == {"phone": "+233 •••• ••• 778", "changing": True, "outcome": "sent"}
    [failed] = await rows(tenant["operator"], "account.password_change_failed")
    assert failed.detail == {"reason": "wrong_current_password"}
    [changed] = await rows(tenant["operator"], "account.password_changed")
    assert changed.changes == {"password": REDACTED}
    for row in (pin, phone, failed, changed):
        assert_actor(row, tenant)
        assert row.target_id == str(admin.id)
        assert_none_of(row, PASSWORD, "Not-The-Password-1", "Brand-New-Pass-42", *ADMIN_PHONE_FORMS,
                       "233244000778", "0244000778", *sent_codes)


async def test_onboarding_rows(env, sent_codes):
    from src.modules.admin_accounts.security_questions import SECURITY_QUESTIONS

    operator = await make_operator()
    async with async_session_factory() as db:
        admin = AdminUser(isp_operator_id=operator.id, email=f"o-{uuid.uuid4().hex[:10]}@throwaway.test",
                          password_hash=hash_password(PASSWORD), role="superadmin", is_active=True,
                          must_complete_onboarding=True, must_change_password=False)
        db.add(admin)
        await db.commit()
    res = await env["client"].post("/auth/login", json={"email": admin.email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    h = {"Authorization": f"Bearer {res.json()['access_token']}"}
    c = env["client"]
    assert (await c.post("/auth/onboarding/phone", headers=h, json={"phone": "0244333999"})).status_code == 200
    assert (await c.post("/auth/onboarding/otp/verify", headers=h, json={"code": "000000" if sent_codes[0] != "000000" else "111111"})).status_code == 400
    assert (await c.post("/auth/onboarding/otp/verify", headers=h, json={"code": sent_codes[0]})).status_code == 200
    question = sorted(SECURITY_QUESTIONS)[0]
    done = await c.post("/auth/onboarding/password", headers=h, json={
        "new_password": "Onboard-Pass-55z", "confirm_password": "Onboard-Pass-55z",
        "security_question": question, "security_answer": "Accra Academy"})
    assert done.status_code == 200, done.text

    t = {"operator": operator, "admin": admin}
    [sent] = await rows(operator, "account.onboarding_code_sent")
    [failed] = await rows(operator, "account.onboarding_code_failed")
    [verified] = await rows(operator, "account.onboarding_phone_verified")
    [completed] = await rows(operator, "account.setup_completed")
    assert sent.detail == {"phone": "+233 •••• ••• 999", "outcome": "sent"}
    assert verified.detail == {"phone": "+233 •••• ••• 999"}
    assert completed.changes == {"password": REDACTED, "security_answer": REDACTED, "security_question": "changed"}
    assert completed.detail == {"mode": "onboarding"}
    for row in (sent, failed, verified, completed):
        assert_actor(row, t)
        assert_none_of(row, PASSWORD, "Onboard-Pass-55z", "Accra Academy", "accra academy", question,
                       "233244333999", "0244333999", "244333999", *sent_codes)


async def test_password_reset_rows(env, tenant, sent_codes):
    from src.modules.admin_accounts.security_questions import SECURITY_QUESTIONS

    admin = tenant["admin"]
    question = sorted(SECURITY_QUESTIONS)[1]
    async with async_session_factory() as db:
        await db.execute(update(AdminUser).where(AdminUser.id == admin.id).values(
            security_question=question, security_answer_hash=hash_password("kumasi")))
        await db.commit()
    c = env["client"]
    # An unknown email: the same answer, and no row anywhere.
    assert (await c.post("/auth/reset/request", json={"email": "nobody-here@throwaway.test"})).status_code == 200
    assert (await c.post("/auth/reset/request", json={"email": admin.email})).status_code == 200
    assert (await c.post("/auth/reset/verify-otp", json={"email": admin.email, "code": "000000" if sent_codes[0] != "000000" else "111111"})).status_code == 400
    granted = await c.post("/auth/reset/verify-otp", json={"email": admin.email, "code": sent_codes[0]})
    assert granted.status_code == 200
    token = granted.json()["reset_token"]
    assert (await c.post("/auth/reset/security-question", json={"email": admin.email})).status_code == 200  # lookup: no row
    assert (await c.post("/auth/reset/security-question", json={"email": admin.email, "answer": "takoradi"})).status_code == 400
    answered = await c.post("/auth/reset/security-question", json={"email": admin.email, "answer": "Kumasi"})
    assert answered.status_code == 200
    done = await c.post("/auth/reset/set-password", json={"reset_token": token, "new_password": "Reset-Pass-66q", "confirm_password": "Reset-Pass-66q"})
    assert done.status_code == 200, done.text

    op = tenant["operator"]
    [requested] = await all_rows(op, "auth.reset_requested")
    [code_failed] = await all_rows(op, "auth.reset_code_failed")
    [code_ok] = await all_rows(op, "auth.reset_code_verified")
    [answer_failed] = await all_rows(op, "auth.reset_answer_failed")
    [answer_ok] = await all_rows(op, "auth.reset_answer_verified")
    [reset] = await all_rows(op, "auth.password_reset")
    # Nobody has proved who they are yet: the system is the actor.
    for row in (requested, code_failed, answer_failed):
        assert (row.actor_type, row.actor_id) == ("system", None) and row.target_id == str(admin.id)
        assert str(row.ip) == "127.0.0.1" and row.user_agent
    for row in (code_ok, answer_ok, reset):
        assert_actor(row, tenant)
    assert requested.detail == {"phone": "+233 •••• ••• 777"}
    assert answer_failed.detail == {"locked": False, "attempts": 1}
    assert reset.changes == {"password": REDACTED} and reset.detail == {"via": "otp"}
    async with async_session_factory() as db:
        stray = (await db.execute(select(AuditLog).where(AuditLog.action.startswith("auth.reset"),
                                                         AuditLog.summary.contains("nobody-here")))).scalars().all()
    assert stray == []
    for row in (requested, code_failed, code_ok, answer_failed, answer_ok, reset):
        assert_none_of(row, token, "Reset-Pass-66q", "kumasi", "Kumasi", "takoradi", *ADMIN_PHONE_FORMS, *sent_codes)


# ── Tenant isolation: ids that name another operator's rows ─────────────────

@pytest_asyncio.fixture(loop_scope="module")
async def other(env):
    """Operator B, with one of everything A might try to point at."""
    operator = await make_operator()
    admin = await make_admin(operator)
    h = await sign_in(env, admin)
    c = env["client"]
    made = await make_site(env, {"h": h})
    plan = await new_plan(env, {"h": h}, price_ghs=float(1 + uuid.uuid4().int % 900))
    reseller = (await c.post("/admin/resellers", headers=h, json={
        "name": "B res", "email": f"b-{uuid.uuid4().hex[:8]}@throwaway.test", "password": "B-Res-Pass-1"})).json()
    template = (await c.post("/admin/config-templates", headers=h, json={
        "name": f"b-tpl-{uuid.uuid4().hex[:6]}", "template_data": {"version": 1, "commands": [{"path": "/ip/dns"}]}})).json()
    voucher = (await c.post("/vouchers/generate", headers=h, json={"plan_id": plan["id"], "quantity": 1})).json()[0]
    return {"operator": operator, "h": h, "town": made["town"]["id"], "site": made["site"]["id"], "plan": plan["id"],
            "reseller": reseller["id"], "template": template["id"], "voucher": voucher["id"]}


async def _same_404(res, missing, label):
    """B's id and an id that exists nowhere get the identical answer."""
    assert res.status_code == 404 and missing.status_code == 404, (res.text, missing.text)
    assert res.json() == missing.json() == {"detail": f"{label} not found"}


async def test_reseller_cannot_point_at_another_operators_town_or_site(env, tenant, other):
    from src.db.models import Reseller

    c, h = env["client"], tenant["h"]
    base = {"name": "A res", "password": "A-Res-Pass-1"}
    for field, label in (("town_id", "Town"), ("site_id", "Site")):
        res = await c.post("/admin/resellers", headers=h, json={**base, "email": f"a-{uuid.uuid4().hex[:8]}@throwaway.test", field: other[field[:-3]]})
        missing = await c.post("/admin/resellers", headers=h, json={**base, "email": f"a-{uuid.uuid4().hex[:8]}@throwaway.test", field: str(uuid.uuid4())})
        await _same_404(res, missing, label)
    junk = await c.post("/admin/resellers", headers=h, json={**base, "email": f"a-{uuid.uuid4().hex[:8]}@throwaway.test", "site_id": "not-a-uuid"})
    assert junk.status_code == 404
    async with async_session_factory() as db:
        assert (await db.execute(select(Reseller).where(Reseller.isp_operator_id == tenant["operator"].id))).scalars().all() == []

    # An edit: refused, and the reseller is untouched.
    own = await make_site(env, tenant)
    rid = (await c.post("/admin/resellers", headers=h, json={**base, "email": f"a-{uuid.uuid4().hex[:8]}@throwaway.test",
                                                            "site_id": own["site"]["id"]})).json()["id"]
    res = await c.put(f"/admin/resellers/{rid}", headers=h, json={"name": "Renamed", "site_id": other["site"]})
    assert res.status_code == 404
    async with async_session_factory() as db:
        row = await db.get(Reseller, uuid.UUID(rid))
    assert (row.name, str(row.site_id)) == ("A res", own["site"]["id"])
    # Clearing a reference is still allowed.
    assert (await c.put(f"/admin/resellers/{rid}", headers=h, json={"site_id": None})).status_code == 200


async def test_provision_cannot_use_another_operators_template(env, tenant, other, fake_router_side):
    from src.db.models import ConfigTemplate, RouterProvisionLog
    from src.modules.mikrotik.template_engine import ConfigTemplateService, TemplateValidationError

    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    await _with_credentials(env, tenant, rid)
    c, h = env["client"], tenant["h"]
    res = await c.post(f"/admin/routers/{rid}/provision", headers=h, json={"dns_name": "wifi.local", "template_id": other["template"]})
    missing = await c.post(f"/admin/routers/{rid}/provision", headers=h, json={"dns_name": "wifi.local", "template_id": str(uuid.uuid4())})
    await _same_404(res, missing, "Config template")
    async with async_session_factory() as db:
        assert (await db.execute(select(RouterProvisionLog).where(RouterProvisionLog.router_id == uuid.UUID(rid)))).scalars().all() == []
        # A platform-wide template is still fine.
        shared = ConfigTemplate(isp_operator_id=None, name=f"global-{uuid.uuid4().hex[:6]}",
                                template_data={"version": 1, "commands": [{"path": "/ip/dns"}]})
        db.add(shared)
        await db.commit()
    ok = await c.post(f"/admin/routers/{rid}/provision", headers=h, json={"dns_name": "wifi.local", "template_id": str(shared.id)})
    assert ok.status_code == 200, ok.text
    # The engine refuses it too, whoever calls it.
    async with async_session_factory() as db:
        with pytest.raises(TemplateValidationError, match="not found"):
            await ConfigTemplateService().apply_template(db, router_id=rid, template_id=other["template"])


async def test_disconnect_cannot_name_another_operators_voucher(env, tenant, other, fake_router_side, monkeypatch):
    from src.db.models import CoAEvent
    from src.modules.mikrotik import routes as mt_routes

    calls = []

    async def disconnect(router_id, active_id):
        calls.append(active_id)

    monkeypatch.setattr(mt_routes.service, "disconnect_hotspot_user", disconnect)
    made = await make_site(env, tenant)
    rid = await onboard(env, tenant, made["site"]["id"])
    c, h = env["client"], tenant["h"]
    res = await c.post(f"/admin/routers/{rid}/disconnect-user", headers=h, json={"active_id": "*1", "voucher_id": other["voucher"]})
    missing = await c.post(f"/admin/routers/{rid}/disconnect-user", headers=h, json={"active_id": "*1", "voucher_id": str(uuid.uuid4())})
    await _same_404(res, missing, "Voucher")
    assert calls == [], "the router was never contacted"
    async with async_session_factory() as db:
        assert (await db.execute(select(CoAEvent).where(CoAEvent.router_id == uuid.UUID(rid)))).scalars().all() == []


# ── Tenant isolation on public routes: portal and payment webhooks ──────────

PORTAL = "http://testserver/portal"


async def _router_token(env, t) -> tuple[str, dict]:
    """A router of t's with a signed portal token, as the captive portal gets it."""
    from src.utils.portal_token import create_portal_router_token

    made = await make_site(env, t)
    rid = await onboard(env, t, made["site"]["id"])
    return create_portal_router_token(rid), made


async def test_portal_sites_lists_only_the_hotspot_operators_sites(env, tenant, other):
    rt, made = await _router_token(env, tenant)
    res = await env["client"].get(f"{PORTAL}/sites", params={"rt": rt})
    assert res.status_code == 200, res.text
    ids = {s["id"] for s in res.json()}
    assert made["site"]["id"] in ids and other["site"] not in ids
    # Nothing to say whose hotspot this is: no list at all.
    assert (await env["client"].get(f"{PORTAL}/sites")).status_code == 400


async def test_portal_sites_branding_preview_lists_the_previewing_operators_sites(env, tenant, other):
    """The Branding page's live preview carries a preview token, not a router
    token, plus the page's default gateway; it gets that operator's own sites."""
    from src.utils.portal_token import create_portal_preview_token

    made = await make_site(env, tenant)
    preview = create_portal_preview_token(str(tenant["operator"].id))
    res = await env["client"].get(f"{PORTAL}/sites", params={"rt": preview, "gateway": "10.5.50.1"})
    assert res.status_code == 200, res.text
    ids = {s["id"] for s in res.json()}
    assert made["site"]["id"] in ids and other["site"] not in ids
    # A preview token only names whose sites to list; it is not a hotspot.
    plan = await new_plan(env, tenant, price_ghs=float(1 + uuid.uuid4().int % 900))
    pay = await env["client"].post(f"{PORTAL}/initiate-payment", json={
        "plan_id": plan["id"], "rt": preview, "gateway": "10.5.50.1",
        "phone": "0244123456", "payment_method": "mtn_momo"})
    assert pay.status_code == 404, pay.text


async def test_portal_payment_cannot_use_another_operators_site(env, tenant, other, monkeypatch):
    from src.db.models import PaymentTransaction

    rt, _ = await _router_token(env, tenant)
    plan = await new_plan(env, tenant, price_ghs=float(1 + uuid.uuid4().int % 900))  # not tied to a site
    body = {"plan_id": plan["id"], "rt": rt, "phone": "0244123456", "payment_method": "mtn_momo"}
    res = await env["client"].post(f"{PORTAL}/initiate-payment", json={**body, "site_id": other["site"]})
    missing = await env["client"].post(f"{PORTAL}/initiate-payment", json={**body, "site_id": str(uuid.uuid4())})
    await _same_404(res, missing, "Site")
    async with async_session_factory() as db:
        assert (await db.execute(select(PaymentTransaction).where(
            PaymentTransaction.isp_operator_id == tenant["operator"].id))).scalars().all() == []


async def test_webhook_signed_by_one_operator_cannot_settle_anothers_payment(env, tenant, other, providers_offered, monkeypatch):
    """Operator A holds its own Paystack key, so it can sign anything. A webhook
    to A's slug naming B's payment must change nothing of B's."""
    import hashlib
    import hmac
    from types import SimpleNamespace

    from src.db.models import PaymentTransaction
    from src.modules.webhooks import processor
    from src.modules.webhooks import routes as webhook_routes

    assert (await put_creds(env, tenant, "payment", "paystack", PAYSTACK)).status_code == 200
    theirs = await make_payment(env, {"h": other["h"], "operator": other["operator"]}, status="pending")
    mine = await make_payment(env, tenant, status="pending")
    jobs = []

    async def pool():
        async def enqueue_job(name, *args):
            jobs.append(args)
        return SimpleNamespace(enqueue_job=enqueue_job)

    monkeypatch.setattr(webhook_routes, "get_redis_pool", pool)
    async with async_session_factory() as db:
        refs = {pid: (await db.get(PaymentTransaction, uuid.UUID(pid))).internal_reference for pid in (theirs, mine)}

    async def deliver(reference, event):
        raw = json.dumps({"event": event, "data": {"reference": reference, "status": "failed"}}).encode()
        sig = hmac.new(PAYSTACK["webhook_secret"].encode(), raw, hashlib.sha512).hexdigest()
        res = await env["client"].post(f"/webhooks/paystack/{tenant['operator'].slug}", content=raw,
                                       headers={"x-paystack-signature": sig, "content-type": "application/json"})
        assert res.status_code == 200, res.text
        await processor.process_webhook_event(None, *jobs[-1])

    await deliver(refs[theirs], "charge.failed")
    await deliver(refs[mine], "charge.failed")
    assert jobs[0][-1] == str(tenant["operator"].id), "the verifying operator rides with the job"
    async with async_session_factory() as db:
        b_tx = await db.get(PaymentTransaction, uuid.UUID(theirs))
        a_tx = await db.get(PaymentTransaction, uuid.UUID(mine))
    assert (b_tx.status, b_tx.webhook_payload) == ("pending", None), "B's payment untouched"
    assert b_tx.provider_reference.startswith("PSK-")
    assert a_tx.status == "failed", "A's own payment still settles"


# ── Batch 5: platform owner ──────────────────────────────────────────────────

OWNER_PHONE = "233244000999"
OWNER_PHONE_FORMS = ("233244000999", "0244000999", "244000999")


async def make_owner(*, setup_pending=False, with_code=False, phone=OWNER_PHONE):
    from src.db.models import PlatformOwner
    from src.modules.platform import character_code

    owner_id = uuid.uuid4()
    code = character_code.generate_code() if with_code else None
    async with async_session_factory() as db:
        owner = PlatformOwner(
            id=owner_id, email=f"owner-{uuid.uuid4().hex[:8]}@throwaway.test", password_hash=hash_password(PASSWORD),
            name="Owner", is_active=True, must_complete_security_setup=setup_pending,
            phone=phone, phone_verified=bool(phone),
            challenge_hashes=character_code.build_storage(owner_id, code) if code else None,
        )
        db.add(owner)
        await db.commit()
    return owner, code


def owner_headers(owner) -> dict:
    from src.modules.auth.tokens import platform_owner_token_data
    from src.utils.auth import create_platform_owner_access_token

    return {"Authorization": f"Bearer {create_platform_owner_access_token(platform_owner_token_data(owner))}"}


@pytest_asyncio.fixture(loop_scope="module")
async def platform(env):
    owner, _ = await make_owner()
    return {"owner": owner, "h": owner_headers(owner)}


async def owner_rows(owner, action=None):
    async with async_session_factory() as db:
        q = select(AuditLog).where(AuditLog.target_id == str(owner.id))
        if action:
            q = q.where(AuditLog.action == action)
        return (await db.execute(q.order_by(AuditLog.created_at))).scalars().all()


async def rows_of(action, target_id):
    async with async_session_factory() as db:
        return (await db.execute(select(AuditLog).where(AuditLog.action == action, AuditLog.target_id == str(target_id))
                                 .order_by(AuditLog.created_at))).scalars().all()


def assert_owner_actor(row, owner, operator_id=None):
    assert (row.actor_type, row.actor_id, row.actor_label) == ("platform_owner", owner.id, owner.email)
    assert row.isp_operator_id == operator_id
    assert str(row.ip) == "127.0.0.1" and row.request_id and row.user_agent


def assert_system_actor(row):
    assert (row.actor_type, row.actor_id, row.actor_label, row.isp_operator_id) == ("system", None, None, None)


@pytest.fixture
def owner_sms(monkeypatch):
    """Every SMS the platform routes would send, captured instead."""
    from types import SimpleNamespace

    from src.modules.admin_accounts import platform_reset
    from src.modules.applications import service as app_service
    from src.modules.notifications import dispatcher
    from src.modules.platform import owner_account_routes, owner_security_routes
    from src.modules.platform import routes as platform_routes

    sent = {"codes": [], "temp_passwords": []}

    async def otp(phone, code, purpose=None, **kw):
        sent["codes"].append(code)
        return SimpleNamespace(success=True, error=None)

    async def temp(admin, temp_password, *a, **kw):
        sent["temp_passwords"].append(temp_password)
        return SimpleNamespace(success=True, error=None)

    async def quiet(*a, **kw):
        return SimpleNamespace(success=True, error=None)

    for module in (owner_account_routes, owner_security_routes):
        monkeypatch.setattr(module, "send_otp_sms", otp)
    monkeypatch.setattr(owner_account_routes, "send_platform_owner_phone_changed_sms", quiet)
    monkeypatch.setattr(owner_account_routes, "send_platform_owner_question_changed_sms", quiet)
    monkeypatch.setattr(platform_routes, "send_temp_password_sms", temp)
    monkeypatch.setattr(app_service, "send_temp_password_sms", temp)
    monkeypatch.setattr(platform_reset.notifications, "send_temp_password_sms", temp)
    monkeypatch.setattr(dispatcher, "notify_application_approved", quiet)
    monkeypatch.setattr(dispatcher, "notify_application_rejected", quiet)
    return sent


async def test_owner_sign_in_rows_password_only(env):
    c = env["client"]
    owner, _ = await make_owner(setup_pending=True)
    assert (await c.post("/platform/auth/login", json={"email": owner.email, "password": "Wrong-Pass-1"})).status_code == 401
    assert (await c.post("/platform/auth/login", json={"email": "nobody@throwaway.test", "password": "x"})).status_code == 401
    ok = await c.post("/platform/auth/login", json={"email": owner.email, "password": PASSWORD})
    assert ok.status_code == 200, ok.text

    [failed] = await owner_rows(owner, "platform_auth.login_failed")
    assert_system_actor(failed)
    assert failed.detail == {"reason": "wrong_password"}
    [login] = await owner_rows(owner, "platform_auth.login")
    assert_owner_actor(login, owner)
    assert login.detail == {"step": "password", "setup_pending": True}
    async with async_session_factory() as db:  # an unknown email writes nothing
        assert not (await db.execute(select(AuditLog).where(AuditLog.summary.contains("nobody@throwaway.test")))).scalars().all()
    for row in (failed, login):
        assert_none_of(row, PASSWORD, "Wrong-Pass-1")

    tokens = ok.json()
    out = await c.post("/platform/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert out.status_code == 204
    [logout] = await owner_rows(owner, "platform_auth.logout")
    assert (logout.actor_type, logout.actor_id, logout.isp_operator_id) == ("platform_owner", owner.id, None)
    assert logout.detail == {"via": "refresh"}
    assert (await c.post("/platform/auth/logout", json={"refresh_token": tokens["refresh_token"]})).status_code == 204
    assert len(await owner_rows(owner, "platform_auth.logout")) == 1  # a replayed sign-out ended nothing


async def test_owner_sign_in_rows_with_the_character_challenge(env):
    c = env["client"]
    owner, code = await make_owner(with_code=True)

    async def password_step():
        res = await c.post("/platform/auth/login", json={"email": owner.email, "password": PASSWORD})
        assert res.status_code == 200 and res.json()["challenge_required"], res.text
        return res.json()

    step = await password_step()
    wrong = {str(p): ("A" if code[p - 1] != "A" else "B") for p in step["positions"]}
    assert (await c.post("/platform/auth/challenge", json={"challenge_token": step["challenge_token"], "characters": wrong})).status_code == 401
    step = await password_step()
    right = {str(p): code[p - 1] for p in step["positions"]}
    ok = await c.post("/platform/auth/challenge", json={"challenge_token": step["challenge_token"], "characters": right})
    assert ok.status_code == 200, ok.text

    issued = await owner_rows(owner, "platform_auth.challenge_issued")
    assert len(issued) == 2
    for row in issued:
        assert_system_actor(row)
    [failed] = await owner_rows(owner, "platform_auth.challenge_failed")
    assert_system_actor(failed)
    assert failed.detail == {"reason": "wrong_characters"}
    [login] = await owner_rows(owner, "platform_auth.login")
    assert_owner_actor(login, owner)
    assert login.detail == {"step": "challenge"}
    for row in (*issued, failed, login):
        assert_none_of(row, PASSWORD, code, step["challenge_token"])


async def test_owner_account_rows(env, owner_sms):
    from src.modules.admin_accounts.security_questions import SECURITY_QUESTIONS

    c = env["client"]
    owner, _ = await make_owner()
    h = owner_headers(owner)
    assert (await c.patch("/platform/me", headers=h, json={"name": "Renamed Owner"})).status_code == 200
    assert (await c.patch("/platform/me", headers=h, json={"name": "Renamed Owner"})).status_code == 200
    assert (await c.post("/platform/me/phone", headers=h, json={"phone": "0244000998", "current_password": "Nope-1"})).status_code == 400
    assert (await c.post("/platform/me/phone", headers=h, json={"phone": "0244000998", "current_password": PASSWORD})).status_code == 200
    assert (await c.post("/platform/me/phone/verify", headers=h, json={"code": "000000"})).status_code == 400
    assert (await c.post("/platform/me/phone/verify", headers=h, json={"code": owner_sms["codes"][-1]})).status_code == 200
    question = next(iter(SECURITY_QUESTIONS))
    res = await c.post("/platform/me/security-question", headers=h, json={
        "security_question": question, "security_answer": "Kumasi Central", "current_password": PASSWORD})
    assert res.status_code == 200, res.text
    wrong = await c.post("/platform/me/password", headers=h, json={
        "current_password": "Nope-1", "new_password": "Brand-New-Pass-42", "confirm_password": "Brand-New-Pass-42"})
    assert wrong.status_code == 400
    ok = await c.post("/platform/me/password", headers=h, json={
        "current_password": PASSWORD, "new_password": "Brand-New-Pass-42", "confirm_password": "Brand-New-Pass-42"})
    assert ok.status_code == 200, ok.text

    [update_row] = await owner_rows(owner, "platform_owner.update")  # the repeat save wrote none
    assert update_row.changes == {"name": {"before": "Owner", "after": "Renamed Owner"}}
    [phone_failed] = await owner_rows(owner, "platform_owner.phone_change_failed")
    assert phone_failed.detail == {"reason": "wrong_current_password"}
    [sent] = await owner_rows(owner, "platform_owner.phone_change_code_sent")
    assert sent.detail == {"phone": "+233 •••• ••• 998", "outcome": "sent"}
    [code_failed] = await owner_rows(owner, "platform_owner.phone_change_code_failed")
    [changed] = await owner_rows(owner, "platform_owner.phone_changed")
    assert changed.detail == {"old_phone": "+233 •••• ••• 999", "new_phone": "+233 •••• ••• 998"}
    [question_row] = await owner_rows(owner, "platform_owner.security_question_changed")
    assert question_row.changes == {"security_answer": REDACTED} and question_row.detail == {"question": question}
    [pw_failed] = await owner_rows(owner, "platform_owner.password_change_failed")
    [pw] = await owner_rows(owner, "platform_owner.password_changed")
    assert pw.changes == {"password": REDACTED}
    for row in (update_row, phone_failed, sent, code_failed, changed, question_row, pw_failed, pw):
        assert_owner_actor(row, owner)
        assert_none_of(row, PASSWORD, "Nope-1", "Brand-New-Pass-42", "Kumasi Central", "kumasi central",
                       *OWNER_PHONE_FORMS, "233244000998", "0244000998", *owner_sms["codes"])


async def test_owner_setup_rows(env, owner_sms):
    from src.modules.admin_accounts.security_questions import SECURITY_QUESTIONS

    c = env["client"]
    owner, _ = await make_owner(setup_pending=True, phone=None)
    h = owner_headers(owner)
    assert (await c.post("/platform/setup/phone", headers=h, json={"phone": "0244000997", "current_password": "Nope-1"})).status_code == 400
    assert (await c.post("/platform/setup/phone", headers=h, json={"phone": "0244000997", "current_password": PASSWORD})).status_code == 200
    assert (await c.post("/platform/setup/phone/verify", headers=h, json={"code": owner_sms["codes"][-1]})).status_code == 200
    question = next(iter(SECURITY_QUESTIONS))
    assert (await c.post("/platform/setup/security-question", headers=h, json={
        "security_question": question, "security_answer": "Kumasi Central", "current_password": PASSWORD})).status_code == 200
    made = await c.post("/platform/setup/character-code", headers=h, json={"current_password": PASSWORD})
    assert made.status_code == 200, made.text
    code = made.json()["character_code"]
    positions = (await c.get("/platform/setup/character-code/check", headers=h)).json()["positions"]
    assert (await c.post("/platform/setup/character-code/check", headers=h,
                         json={"characters": {str(p): code[p - 1] for p in positions}})).status_code == 200
    done = await c.post("/platform/setup/complete", headers=h, json={"current_password": PASSWORD})
    assert done.status_code == 200, done.text

    actions = [row.action for row in await owner_rows(owner)]
    assert actions == ["platform_owner.setup_step_failed", "platform_owner.setup_code_sent",
                       "platform_owner.setup_phone_verified", "platform_owner.setup_security_question",
                       "platform_owner.setup_character_code", "platform_owner.setup_completed"]
    for row in await owner_rows(owner):
        assert_owner_actor(row, owner)
        assert_none_of(row, PASSWORD, "Nope-1", code, "Kumasi Central", "kumasi central",
                       "233244000997", "0244000997", *owner_sms["codes"])


async def test_operator_management_rows(env, platform, owner_sms):
    c, h, owner = env["client"], platform["h"], platform["owner"]
    slug = f"p5-{uuid.uuid4().hex[:8]}"
    res = await c.post("/platform/operators", headers=h, json={
        "name": "P5 Net", "slug": slug, "contact_email": f"{slug}@throwaway.test", "contact_phone": "0207770001",
        "initial_admin_email": f"adm-{slug}@throwaway.test", "initial_admin_phone": "0244000555", "trial_days": 14})
    assert res.status_code == 201, res.text
    op_id = uuid.UUID(res.json()["id"])
    admin_id = res.json()["initial_admin"]["id"]
    assert (await c.patch(f"/platform/operators/{op_id}", headers=h, json={"name": "P5 Net Ltd", "contact_phone": "0207770002"})).status_code == 200
    assert (await c.patch(f"/platform/operators/{op_id}", headers=h, json={"name": "P5 Net Ltd"})).status_code == 200
    assert (await c.put(f"/platform/operators/{op_id}/status", headers=h, json={"status": "suspended"})).status_code == 200
    assert (await c.put(f"/platform/operators/{op_id}/billing", headers=h, json={"monthly_fee_ghs": "75.00", "extend_trial_days": 7})).status_code == 200
    added = await c.post(f"/platform/operators/{op_id}/admins", headers=h, json={
        "email": f"adm2-{slug}@throwaway.test", "phone": "0244000556", "role": "admin"})
    assert added.status_code == 201, added.text
    reset = await c.post(f"/platform/operators/{op_id}/admins/{admin_id}/reset-password", headers=h, json={})
    assert reset.status_code == 200, reset.text

    async with async_session_factory() as db:
        op_rows = (await db.execute(select(AuditLog).where(AuditLog.isp_operator_id == op_id)
                                    .order_by(AuditLog.created_at))).scalars().all()
    # Rows from one transaction share a timestamp, so compare as a multiset.
    assert sorted(r.action for r in op_rows) == sorted([
        "operator.create", "admin_user.create", "operator.update", "operator.status_change",
        "operator.billing_update", "admin_user.create", "admin_user.password_reset"])
    one = {r.action: r for r in op_rows}
    create, update_row, status_row, billing, reset_row = (one[a] for a in (
        "operator.create", "operator.update", "operator.status_change", "operator.billing_update", "admin_user.password_reset"))
    assert create.changes["name"] == {"before": None, "after": "P5 Net"} and create.changes["contact_phone"] == "changed"
    assert update_row.changes == {"name": {"before": "P5 Net", "after": "P5 Net Ltd"}, "contact_phone": "changed"}
    assert status_row.changes["status"] == {"before": "approved", "after": "suspended"}
    assert set(billing.changes) == {"monthly_fee_ghs", "trial_ends_at"}
    assert reset_row.changes == {"password": REDACTED} and reset_row.target_id == admin_id
    for row in op_rows:
        assert_owner_actor(row, owner, op_id)
        assert_none_of(row, *owner_sms["temp_passwords"], "233207770001", "0207770001", "233207770002", "0207770002",
                       "233244000555", "0244000555", "233244000556", "0244000556")


async def test_operator_status_fails_closed(env, platform, tenant, monkeypatch):
    break_audit(monkeypatch)
    res = await env["client"].put(f"/platform/operators/{tenant['operator'].id}/status", headers=platform["h"],
                                  json={"status": "suspended"})
    assert res.status_code == 500
    async with async_session_factory() as db:
        assert (await db.get(ISPOperator, tenant["operator"].id)).status == "approved"


async def test_application_rows(env, platform, owner_sms):
    from src.db.models import OperatorApplication

    c, h, owner = env["client"], platform["h"], platform["owner"]
    ids = []
    async with async_session_factory() as db:
        for _ in range(2):
            tag = uuid.uuid4().hex[:8]
            app_row = OperatorApplication(isp_name=f"Apply {tag}", contact_name="Ama", email=f"apply-{tag}@throwaway.test",
                                          phone="233244000444", region="Volta")
            db.add(app_row)
            await db.flush()
            ids.append(app_row.id)
        await db.commit()
    approved = await c.put(f"/platform/applications/{ids[0]}/approve", headers=h)
    assert approved.status_code == 200, approved.text
    assert (await c.put(f"/platform/applications/{ids[1]}/reject", headers=h,
                        json={"rejection_reason": "Outside our coverage"})).status_code == 200

    op_id = uuid.UUID(approved.json()["operator_id"])
    [approve] = await rows_of("application.approve", ids[0])
    assert_owner_actor(approve, owner, op_id)
    async with async_session_factory() as db:
        made = (await db.execute(select(AuditLog.action).where(AuditLog.isp_operator_id == op_id)
                                 .order_by(AuditLog.created_at))).scalars().all()
    assert sorted(made) == ["admin_user.create", "application.approve", "operator.create"]
    [reject] = await rows_of("application.reject", ids[1])
    assert_owner_actor(reject, owner)
    assert reject.detail == {"reason": "Outside our coverage"}
    for row in (approve, reject):
        assert_none_of(row, *owner_sms["temp_passwords"], "233244000444", "0244000444")


async def test_billing_rows_land_in_the_operators_trail(env, platform, tenant):
    from decimal import Decimal

    from src.db.models import OperatorInvoice, SMSUsageRecord

    c, h, owner, op = env["client"], platform["h"], platform["owner"], tenant["operator"]
    pid = await make_payment(env, tenant)
    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        invoice = OperatorInvoice(isp_operator_id=op.id, invoice_number=f"INV-P5-{uuid.uuid4().hex[:6]}",
                                  period_start=now - timedelta(days=30), period_end=now, amount_ghs=Decimal("50.00"),
                                  status="issued", issued_at=now)
        usage = SMSUsageRecord(isp_operator_id=op.id, segment_count=1, rate_ghs_per_segment=Decimal("0.05"),
                               amount_ghs=Decimal("0.05"))
        db.add_all([invoice, usage])
        await db.commit()
    for _ in range(2):  # the second, unchanged, writes nothing
        assert (await c.patch(f"/platform/payment-transactions/{pid}/diagnostic", headers=h, json={"is_diagnostic": True})).status_code == 200
        assert (await c.patch(f"/platform/sms-usage-records/{usage.id}/diagnostic", headers=h, json={"is_diagnostic": True})).status_code == 200
    assert (await c.put(f"/platform/invoices/{invoice.id}/waive", headers=h)).status_code == 200

    [tx_row] = await rows_of("payment.diagnostic_flag", pid)
    [sms_row] = await rows_of("sms_usage.diagnostic_flag", usage.id)
    [waive] = await rows_of("invoice.waive", invoice.id)
    for row in (tx_row, sms_row):
        assert row.changes == {"is_diagnostic": {"before": False, "after": True}}
    assert waive.changes == {"status": {"before": "issued", "after": "waived"}}
    for row in (tx_row, sms_row, waive):
        assert_owner_actor(row, owner, op.id)
        assert_none_of(row, PHONE)


async def test_platform_wide_setting_rows(env, platform):
    from src.db.models import ProviderCatalogEntry
    from src.modules.notifications import template_catalog as catalog

    c, h, owner = env["client"], platform["h"], platform["owner"]
    support = f"support-{uuid.uuid4().hex[:6]}@throwaway.test"
    for _ in range(2):  # each second save changes nothing and writes nothing
        assert (await c.put("/platform/settings", headers=h, json={"platform_support_email": support})).status_code == 200
        assert (await c.put("/platform/billing/default-fee", headers=h, json={"default_monthly_fee_ghs": "123.00"})).status_code == 200
    async with async_session_factory() as db:
        entry = (await db.execute(select(ProviderCatalogEntry).where(ProviderCatalogEntry.is_integrated == True)  # noqa: E712
                                  .limit(1))).scalar_one()
    flipped = not entry.is_available
    for value in (flipped, flipped, entry.is_available):
        assert (await c.put(f"/platform/providers/{entry.id}", headers=h, json={"is_available": value})).status_code == 200

    event, channel = next(key for key in catalog.DEFAULTS if key[1] == catalog.SMS)
    default = catalog.DEFAULTS[(event, channel)]
    edited = default.body_text.rstrip(".!") + "!"
    saved = await c.put(f"/platform/notification-templates/{event}/{channel}", headers=h, json={"body_text": edited})
    assert saved.status_code == 200, saved.text
    for _ in range(2):
        assert (await c.post(f"/platform/notification-templates/{event}/{channel}/reset", headers=h)).status_code == 200

    async with async_session_factory() as db:
        mine = (await db.execute(select(AuditLog).where(AuditLog.actor_id == owner.id)
                                 .order_by(AuditLog.created_at))).scalars().all()
    by_action = {}
    for row in mine:
        by_action.setdefault(row.action, []).append(row)
    [settings] = by_action["platform.settings_update"]
    assert settings.changes["platform_support_email"]["after"] == support
    [fee] = by_action["platform.default_fee_update"]
    assert fee.changes["default_monthly_fee_ghs"]["after"] in ("123.00", 123.0, "123")
    assert len(by_action["provider_catalog.update"]) == 2
    [update_row] = by_action["notification_template.update"]
    assert update_row.target_id == f"{event}/{channel}"
    assert update_row.changes == {"body_text": {"before": default.body_text, "after": edited}}
    [reset_row] = by_action["notification_template.reset"]  # resetting the default again wrote none
    assert reset_row.changes == {"body_text": {"before": edited, "after": default.body_text}}
    for row in mine:
        assert_owner_actor(row, owner)


async def test_platform_credential_rows_never_carry_a_key(env, platform, monkeypatch):
    import httpx as httpx_module

    from src.modules.sms.providers import arkesel

    c, h, owner = env["client"], platform["h"], platform["owner"]
    pk, sk, wh = (f"pk_test_{uuid.uuid4().hex}", f"sk_test_{uuid.uuid4().hex}", f"whsec-{uuid.uuid4().hex}")
    gw_key, note_key = f"PlatGw-{uuid.uuid4().hex}", f"PlatNote-{uuid.uuid4().hex}"
    for _ in range(2):  # an identical second save writes nothing
        assert (await c.put("/platform/payment-credentials", headers=h, json={
            "public_key": pk, "secret_key": sk, "webhook_secret": wh})).status_code == 200
        assert (await c.put("/platform/sms-credentials", headers=h, json={"api_key": gw_key, "sender_id": "IPADMIN"})).status_code == 200
        assert (await c.put("/platform/notification-sms-credentials", headers=h, json={
            "api_key": note_key, "sender_id": "IPNOTE", "shares_gateway_account": False})).status_code == 200

    class EchoingClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            raise RuntimeError(f"401 Invalid key {sk} (pk {pk})")

        async def __aexit__(self, *a):
            return False

    class EchoingArkesel:
        def __init__(self, *, api_key, sender_id, **kw):
            self.api_key = api_key

        async def verify_credentials(self):
            raise RuntimeError(f"Arkesel 401: api key {self.api_key} is not valid")

    monkeypatch.setattr(httpx_module, "AsyncClient", EchoingClient)
    monkeypatch.setattr(arkesel, "ArkeselSMSProvider", EchoingArkesel)
    for path in ("payment-credentials", "sms-credentials", "notification-sms-credentials"):
        res = await c.post(f"/platform/{path}/test", headers=h)
        assert res.status_code == 400
        assert sk not in res.text and gw_key not in res.text and note_key not in res.text

    async with async_session_factory() as db:
        mine = (await db.execute(select(AuditLog).where(AuditLog.actor_id == owner.id,
                                                        AuditLog.action.like("platform_%credentials.%"))
                                 .order_by(AuditLog.created_at))).scalars().all()
    assert [r.action for r in mine] == [
        "platform_payment_credentials.set", "platform_sms_credentials.set", "platform_notification_sms_credentials.set",
        "platform_payment_credentials.test", "platform_sms_credentials.test", "platform_notification_sms_credentials.test",
    ]
    pay_set, gw_set, note_set, *tests = mine
    assert {k: pay_set.changes[k] for k in ("public_key", "secret_key", "webhook_secret")} == dict.fromkeys(
        ("public_key", "secret_key", "webhook_secret"), REDACTED)
    assert gw_set.changes["api_key"] == REDACTED
    assert gw_set.changes["sender_id"]["after"] == "IPADMIN"
    assert note_set.changes["shares_gateway_account"]["after"] is False
    for row in tests:
        assert row.detail["outcome"] == "failed" and REDACTED in row.detail["error"]
    for row in mine:
        assert_owner_actor(row, owner)
        assert_none_of(row, pk, sk, wh, gw_key, note_key, sk[:12], gw_key[:12])



async def test_webhook_job_without_an_operator_is_dropped(env, tenant, caplog):
    """Nothing is settled by reference alone: a job with no operator id (only a
    leftover from before 2026-10-09 could be one) is logged and dropped."""
    import logging

    from src.db.models import PaymentTransaction
    from src.modules.webhooks import processor

    pid = await make_payment(env, tenant, status="pending")
    async with async_session_factory() as db:
        reference = (await db.get(PaymentTransaction, uuid.UUID(pid))).internal_reference
    raw = json.dumps({"event": "charge.success", "data": {"reference": reference, "status": "success"}})
    with caplog.at_level(logging.ERROR, logger=processor.logger.name):
        await processor.process_webhook_event(None, "paystack", reference, "success", None, raw)
        await processor.process_webhook_event(None, "paystack", reference, "success", None, raw, "")
    async with async_session_factory() as db:
        tx = await db.get(PaymentTransaction, uuid.UUID(pid))
        assert (tx.status, tx.voucher_id, tx.webhook_payload) == ("pending", None, None)
    dropped = [r for r in caplog.records if "Webhook job dropped: no operator id" in r.getMessage()]
    assert len(dropped) == 2 and reference in dropped[0].getMessage()

# ── Batch 6: system jobs ─────────────────────────────────────────────────────


def assert_system_row(row, operator_id):
    assert (row.actor_type, row.actor_id, row.actor_label) == ("system", None, None)
    assert row.isp_operator_id == operator_id


async def billed_operator(*, fee="50.00", **fields):
    from decimal import Decimal

    operator = await make_operator()
    async with async_session_factory() as db:
        op = await db.get(ISPOperator, operator.id)
        op.monthly_fee_ghs = Decimal(fee)
        for name, value in fields.items():
            setattr(op, name, value)
        await db.commit()
    return operator


@pytest.fixture
def quiet_billing_notices(monkeypatch):
    from src.modules.notifications import dispatcher

    async def quiet(*a, **kw):
        return None

    for name in ("notify_grace_period", "notify_suspended", "notify_invoice_issued", "notify_trial_expired",
                 "notify_trial_expiry_warning", "notify_payment_received", "notify_reactivated"):
        if hasattr(dispatcher, name):
            monkeypatch.setattr(dispatcher, name, quiet)


async def overdue_invoice(operator, *, days_late):
    from decimal import Decimal

    from src.db.models import OperatorInvoice

    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        invoice = OperatorInvoice(isp_operator_id=operator.id, invoice_number=f"INV-B6-{uuid.uuid4().hex[:6]}",
                                  period_start=now - timedelta(days=60), period_end=now - timedelta(days=30),
                                  amount_ghs=Decimal("50.00"), status="issued", issued_at=now - timedelta(days=60),
                                  due_at=now - timedelta(days=days_late))
        db.add(invoice)
        await db.commit()
        return invoice


async def test_billing_enforcement_rows(env, quiet_billing_notices):
    from src.config import get_settings
    from src.db.models import OperatorInvoice
    from src.jobs.billing_enforcement import enforce_billing

    operator = await billed_operator(billing_status="active")
    invoice = await overdue_invoice(operator, days_late=get_settings().grace_period_days + 2)
    await enforce_billing()

    [overdue] = await rows_of("invoice.overdue", invoice.id)
    assert overdue.changes == {"status": {"before": "issued", "after": "overdue"}}
    [suspend] = await rows_of("operator.suspend", operator.id)
    assert suspend.changes == {"status": {"before": "approved", "after": "suspended"},
                               "billing_status": {"before": "active", "after": "past_due"},
                               "suspension_reason": {"before": None, "after": "billing"}}
    assert suspend.detail["invoice_number"] == invoice.invoice_number
    for row in (overdue, suspend):
        assert_system_row(row, operator.id)
    await enforce_billing()  # already suspended and overdue: nothing new
    assert len(await rows_of("operator.suspend", operator.id)) == 1
    async with async_session_factory() as db:
        assert (await db.get(OperatorInvoice, invoice.id)).status == "overdue"


async def test_billing_enforcement_fails_closed(env, quiet_billing_notices, monkeypatch):
    """An audit row the database refuses rolls the run back: no suspension
    without its row. (The failure a real outage produces: at commit.)"""
    from src.config import get_settings
    from src.jobs.billing_enforcement import enforce_billing
    from src.modules.audit import service as audit_service

    operator = await billed_operator(billing_status="active")
    await overdue_invoice(operator, days_late=get_settings().grace_period_days + 2)
    real_build = audit_service.build

    def refused(*a, **kw):
        row = real_build(*a, **kw)
        row.actor_type = "refused-by-check-constraint"
        return row

    monkeypatch.setattr(audit_service, "build", refused)
    await enforce_billing()
    async with async_session_factory() as db:
        op = await db.get(ISPOperator, operator.id)
        assert (op.status, op.billing_status) == ("approved", "active")


async def test_payment_after_suspension_rows(env):
    from src.db.models import OperatorInvoice
    from src.modules.billing import service as billing_service

    operator = await billed_operator(status="suspended", billing_status="past_due", suspension_reason="billing")
    invoice = await overdue_invoice(operator, days_late=30)
    async with async_session_factory() as db:
        inv = await db.get(OperatorInvoice, invoice.id)
        op = await db.get(ISPOperator, operator.id)
        await billing_service.mark_invoice_paid(db, inv, "PSK-REF-1")
        await billing_service.normalise_billing_status(db, op)
        await billing_service.reactivate_operator(db, op)
        await db.commit()

    [paid] = await rows_of("invoice.paid", invoice.id)
    assert paid.changes == {"status": {"before": "issued", "after": "paid"}}
    [billing] = await rows_of("operator.billing_status_change", operator.id)
    assert billing.changes == {"billing_status": {"before": "past_due", "after": "active"}}
    [reactivate] = await rows_of("operator.reactivate", operator.id)
    assert reactivate.changes == {"status": {"before": "suspended", "after": "approved"},
                                  "suspension_reason": {"before": "billing", "after": None}}
    for row in (paid, billing, reactivate):
        assert_system_row(row, operator.id)


async def test_trial_end_and_invoice_rows(env, quiet_billing_notices):
    from src.jobs.invoice_generation import generate_monthly_invoices
    from src.jobs.trial_expiry import handle_trial_expiry

    ended = await billed_operator(billing_status="trial", trial_ends_at=datetime.now(timezone.utc) - timedelta(hours=1))
    await handle_trial_expiry()
    [trial] = await rows_of("operator.trial_end", ended.id)
    assert trial.changes == {"billing_status": {"before": "trial", "after": "active"}}
    assert trial.detail["invoiced"] is True
    async with async_session_factory() as db:
        [issued] = (await db.execute(select(AuditLog).where(AuditLog.action == "invoice.issue",
                                                             AuditLog.isp_operator_id == ended.id))).scalars().all()
    assert issued.detail["via"] == "trial_end" and issued.detail["amount_ghs"] in ("50.00", 50.0)

    monthly = await billed_operator(billing_status="active")
    await generate_monthly_invoices()
    await generate_monthly_invoices()  # an invoice for the period exists: no second one, no second row
    async with async_session_factory() as db:
        [month] = (await db.execute(select(AuditLog).where(AuditLog.action == "invoice.issue",
                                                            AuditLog.isp_operator_id == monthly.id))).scalars().all()
    assert month.detail["via"] == "monthly"
    for row in (trial, issued):
        assert_system_row(row, ended.id)
    assert_system_row(month, monthly.id)


async def test_payment_status_rows_by_the_system(env, tenant, monkeypatch):
    from src.db.models import PaymentTransaction
    from src.jobs.payment_reconciliation import run_payment_reconciliation
    from src.modules.payments import service as payment_service_module
    from src.modules.payments.dependencies import get_payment_service

    async def no_sms(payment_id):
        return None

    monkeypatch.setattr(payment_service_module, "enqueue_voucher_sms", no_sms)
    paid, stale = await make_payment(env, tenant, status="pending"), await make_payment(env, tenant, status="pending")
    async with async_session_factory() as db:
        tx = await db.get(PaymentTransaction, uuid.UUID(stale))
        tx.provider_reference = None
        tx.initiated_at = datetime.now(timezone.utc) - timedelta(hours=3)
        await db.commit()
        reference = (await db.get(PaymentTransaction, uuid.UUID(paid))).internal_reference

    async with async_session_factory() as db:
        await get_payment_service().resolve_successful_payment(db, internal_reference=reference, trigger_source="webhook")
    await run_payment_reconciliation()

    [success] = await rows_of("payment.status_change", paid)
    assert success.changes == {"status": {"before": "pending", "after": "success"}}
    assert success.detail["trigger"] == "webhook" and success.detail["code_suffix"].startswith("…")
    [timeout] = await rows_of("payment.status_change", stale)
    assert timeout.changes == {"status": {"before": "pending", "after": "failed"}}
    assert timeout.detail["trigger"] == "timeout" and timeout.detail["failure_reason"] == "timeout"
    async with async_session_factory() as db:
        voucher = (await db.execute(select(Voucher).join(PaymentTransaction, PaymentTransaction.voucher_id == Voucher.id)
                                    .where(PaymentTransaction.id == uuid.UUID(paid)))).scalar_one()
    for row in (success, timeout):
        assert_system_row(row, tenant["operator"].id)
        assert_none_of(row, *PHONE_FORMS, voucher.code, voucher.password, voucher.username)


async def test_voucher_expiry_rows(env, tenant, monkeypatch):
    from src.jobs import voucher_expiry

    async def no_router_cleanup(db, voucher):
        return {}

    monkeypatch.setattr(voucher_expiry, "revoke_voucher_access", no_router_cleanup)
    plan = await new_plan(env, tenant, price_ghs=float(1 + uuid.uuid4().int % 900))
    made = (await env["client"].post("/vouchers/generate", headers=tenant["h"],
                                     json={"plan_id": plan["id"], "quantity": 1})).json()[0]
    async with async_session_factory() as db:
        voucher = await db.get(Voucher, uuid.UUID(made["id"]))
        voucher.status = "active"
        voucher.activated_at = datetime.now(timezone.utc) - timedelta(hours=2)
        voucher.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()
        code, username, password = voucher.code, voucher.username, voucher.password

    await voucher_expiry.expire_vouchers()
    [row] = await rows_of("voucher.expire", made["id"])
    assert row.changes == {"status": {"before": "active", "after": "expired"}}
    from src.modules.audit.redact import mask_code

    assert row.detail["via"] == "expiry_job" and row.detail["code_suffix"] == mask_code(code)
    assert_system_row(row, tenant["operator"].id)
    assert_none_of(row, code, username, password)
    await voucher_expiry.expire_vouchers()
    assert len(await rows_of("voucher.expire", made["id"])) == 1


# Customer-facing paths fail OPEN: expiry, accounting and payment completion
# never depend on the audit write. Billing jobs stay fail-closed (above).


def refuse_audit_inserts(monkeypatch):
    """Every audit row is refused by the database (a CHECK violation at insert),
    the way a broken audit table would refuse it."""
    from src.modules.audit import service as audit_service

    real_build = audit_service.build

    def refused(*a, **kw):
        row = real_build(*a, **kw)
        row.actor_type = "refused-by-check-constraint"
        return row

    monkeypatch.setattr(audit_service, "build", refused)


def assert_logged_failure(caplog, action, target_id):
    lines = [r.getMessage() for r in caplog.records if r.name == "audit" and r.levelname == "ERROR"]
    assert any(f"action={action} target_id={target_id}" in line for line in lines), lines


async def active_voucher(env, t, plan, **fields):
    made = (await env["client"].post("/vouchers/generate", headers=t["h"],
                                     json={"plan_id": plan["id"], "quantity": 1})).json()[0]
    async with async_session_factory() as db:
        voucher = await db.get(Voucher, uuid.UUID(made["id"]))
        voucher.status = "active"
        voucher.activated_at = datetime.now(timezone.utc) - timedelta(hours=2)
        for name, value in fields.items():
            setattr(voucher, name, value)
        await db.commit()
    return made["id"]


async def test_voucher_expiry_fails_open(env, tenant, monkeypatch, caplog):
    import logging

    from src.jobs import voucher_expiry

    async def no_router_cleanup(db, voucher):
        return {}

    monkeypatch.setattr(voucher_expiry, "revoke_voucher_access", no_router_cleanup)
    plan = await new_plan(env, tenant, price_ghs=float(1 + uuid.uuid4().int % 900))
    vid = await active_voucher(env, tenant, plan, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    refuse_audit_inserts(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="audit"):
        await voucher_expiry.expire_vouchers()

    async with async_session_factory() as db:
        assert (await db.get(Voucher, uuid.UUID(vid))).status == "expired"
    assert await rows_of("voucher.expire", vid) == []
    assert_logged_failure(caplog, "voucher.expire", vid)


async def test_payment_completion_fails_open(env, tenant, monkeypatch, caplog):
    import logging

    from src.db.models import PaymentTransaction
    from src.jobs.payment_reconciliation import run_payment_reconciliation
    from src.modules.payments import service as payment_service_module
    from src.modules.payments.dependencies import get_payment_service

    async def no_sms(payment_id):
        return None

    monkeypatch.setattr(payment_service_module, "enqueue_voucher_sms", no_sms)
    paid, stale = await make_payment(env, tenant, status="pending"), await make_payment(env, tenant, status="pending")
    async with async_session_factory() as db:
        tx = await db.get(PaymentTransaction, uuid.UUID(stale))
        tx.provider_reference = None
        tx.initiated_at = datetime.now(timezone.utc) - timedelta(hours=3)
        await db.commit()
        reference = (await db.get(PaymentTransaction, uuid.UUID(paid))).internal_reference

    refuse_audit_inserts(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="audit"):
        async with async_session_factory() as db:
            await get_payment_service().resolve_successful_payment(db, internal_reference=reference, trigger_source="webhook")
        await run_payment_reconciliation()

    async with async_session_factory() as db:
        done = await db.get(PaymentTransaction, uuid.UUID(paid))
        assert done.status == "success" and done.voucher_id is not None
        assert (await db.get(Voucher, done.voucher_id)).status == "unused"
        assert (await db.get(PaymentTransaction, uuid.UUID(stale))).status == "failed"
    assert await rows_of("payment.status_change", paid) == []
    assert_logged_failure(caplog, "payment.status_change", paid)
    assert_logged_failure(caplog, "payment.status_change", stale)


async def test_radius_accounting_fails_open(env, tenant, fake_router_side, monkeypatch, caplog):
    import logging
    from types import SimpleNamespace

    from src.db.models import Session
    from src.radius import accounting

    async def no_disconnect(*a, **kw):
        return SimpleNamespace(status="sent", attempt_count=1)

    monkeypatch.setattr(accounting, "create_pending_disconnect_event", no_disconnect)
    monkeypatch.setattr(accounting, "send_disconnect_with_event", no_disconnect)
    made = await make_site(env, tenant)
    rid = uuid.UUID(await onboard(env, tenant, made["site"]["id"]))
    plan = await new_plan(env, tenant, type="hybrid", data_limit_mb=1, price_ghs=float(1 + uuid.uuid4().int % 900))

    async def exhaust_over_accounting(vid):
        sid = f"acct-{uuid.uuid4().hex[:10]}"
        async with async_session_factory() as db:
            voucher = await db.get(Voucher, uuid.UUID(vid))
            db.add(Session(isp_operator_id=tenant["operator"].id, voucher_id=voucher.id, router_id=rid,
                           username=voucher.username, session_id=sid, nas_ip="10.100.0.250",
                           started_at=datetime.now(timezone.utc), upload_bytes=0, download_bytes=0))
            await db.commit()
        async with async_session_factory() as db:
            await accounting.handle_accounting_interim_update(db, {
                "acct_session_id": sid, "nas_ip_address": "10.100.0.250",
                "acct_input_octets": 2 * 1024 * 1024, "acct_output_octets": 0})
        async with async_session_factory() as db:
            session = (await db.execute(select(Session).where(Session.session_id == sid))).scalar_one()
            assert session.upload_bytes == 2 * 1024 * 1024  # accounting recorded
            assert (await db.get(Voucher, uuid.UUID(vid))).status == "exhausted"
        return vid

    # Both vouchers made first: generating one is an audited, fail-closed route.
    written, refused = await active_voucher(env, tenant, plan), await active_voucher(env, tenant, plan)
    await exhaust_over_accounting(written)
    [row] = await rows_of("voucher.exhaust", written)
    assert row.detail["via"] == "accounting"
    assert_system_row(row, tenant["operator"].id)

    refuse_audit_inserts(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="audit"):
        await exhaust_over_accounting(refused)
    assert await rows_of("voucher.exhaust", refused) == []
    assert_logged_failure(caplog, "voucher.exhaust", refused)
