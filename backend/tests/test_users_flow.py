"""Roles and operator user management (P3) against a REAL database.

/api/v1/admin/users end to end: who may call it, the tenant, superadmin and
self guards, the PIN gate, disable/enable through P2's access helpers, the
audit rows, and GET /auth/me. Runs the FastAPI app in-process. Only runs
against a disposable database at migration 057 or later:

    USERS_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("USERS_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs USERS_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import select, update

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import AdminUser, AuditLog, ISPOperator, PlatformOwner
    from src.middleware.auth import PIN_REQUIRED_HEADER
    from src.middleware.permissions import PERMISSION_DENIED, ROLE_PERMISSIONS
    from src.modules.admin_accounts import notifications as admin_notifications
    from src.modules.admin_users import routes as users_routes
    from src.modules.auth.tokens import platform_owner_token_data
    from src.modules.platform import routes as platform_routes
    from src.utils.auth import create_platform_owner_access_token, hash_password

PASSWORD = "Str0ngPassword"
PHONE = "233244000777"
USERS = "/admin/users"


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    async def no_limit(*args, **kwargs):
        return None

    # Every module that imported the limiter: the throwaway has no Redis.
    for name, module in list(sys.modules.items()):
        if name.startswith("src.") and hasattr(module, "enforce_rate_limit"):
            monkeypatch.setattr(module, "enforce_rate_limit", no_limit)

    texts: list[tuple[str, str]] = []

    async def capture_temp_sms(admin, temp_password, **kwargs):
        from src.modules.sms.types import SMSSendResult
        texts.append((admin.email, temp_password))
        return SMSSendResult(success=True, provider_reference="test")

    monkeypatch.setattr(users_routes, "send_temp_password_sms", capture_temp_sms)
    monkeypatch.setattr(platform_routes, "send_temp_password_sms", capture_temp_sms)
    monkeypatch.setattr(admin_notifications, "send_temp_password_sms", capture_temp_sms)

    notices: list[dict] = []

    async def capture_notice(**kwargs):
        notices.append(kwargs)

    monkeypatch.setattr(platform_routes.notify, "notify_admin_access_changed", capture_notice)

    async with async_session_factory() as db:
        owner = PlatformOwner(
            email=f"owner-{uuid.uuid4().hex[:8]}@throwaway.test", password_hash=hash_password(PASSWORD),
            name="Owner", is_active=True, must_complete_security_setup=False,
        )
        db.add(owner)
        await db.commit()
    owner_token = create_platform_owner_access_token(platform_owner_token_data(owner))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as client:
        yield {"client": client, "owner_headers": bearer(owner_token), "texts": texts, "notices": notices}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def make_operator() -> "ISPOperator":
    slug = f"usr-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Usr {slug}", slug=slug, contact_email=f"{slug}@throwaway.test", status="approved")
        db.add(operator)
        await db.commit()
        return operator


async def make_admin(operator, *, role="superadmin", pin=True, **values) -> "AdminUser":
    """An onboarded admin; with ``pin`` it also has a PIN and is elevated."""
    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        state = dict(
            isp_operator_id=operator.id, email=f"u-{uuid.uuid4().hex[:10]}@throwaway.test",
            password_hash=hash_password(PASSWORD), role=role, is_active=True, phone=PHONE, phone_verified=True,
            must_complete_onboarding=False, must_change_password=False,
        )
        if pin:
            state.update(pin_hash=hash_password("482913"), pin_set_at=now, pin_verified_until=now + timedelta(minutes=10))
        state.update(values)
        admin = AdminUser(**state)
        db.add(admin)
        await db.commit()
        return admin


async def headers(env, admin) -> dict:
    res = await env["client"].post("/auth/login", json={"email": admin.email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    # Sign-in ends any PIN elevation; put back the state the test created.
    async with async_session_factory() as db:
        await db.execute(update(AdminUser).where(AdminUser.id == admin.id).values(pin_verified_until=admin.pin_verified_until))
        await db.commit()
    return bearer(res.json()["access_token"])


async def fresh(admin) -> "AdminUser":
    async with async_session_factory() as db:
        return await db.get(AdminUser, admin.id)


async def audit_rows(target_id, action):
    async with async_session_factory() as db:
        return (await db.execute(
            select(AuditLog).where(AuditLog.target_id == str(target_id), AuditLog.action == action)
        )).scalars().all()


def new_user_body(role="staff", **extra):
    return {"email": f"new-{uuid.uuid4().hex[:10]}@throwaway.test", "phone": "0244000888", "role": role, **extra}


def denied(res) -> bool:
    return res.status_code == 403 and res.json().get("detail") == PERMISSION_DENIED


# ── GET /auth/me ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["staff", "admin", "superadmin"])
async def test_me_returns_role_and_permissions(env, role):
    operator = await make_operator()
    admin = await make_admin(operator, role=role)
    res = await env["client"].get("/auth/me", headers=await headers(env, admin))
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["role"], body["email"], body["has_pin"]) == (role, admin.email, True)
    assert set(body["permissions"]) == set(ROLE_PERMISSIONS[role])


async def test_me_reports_a_missing_pin(env):
    operator = await make_operator()
    admin = await make_admin(operator, pin=False)
    res = await env["client"].get("/auth/me", headers=await headers(env, admin))
    assert res.json()["has_pin"] is False


# ── Who may call /admin/users ───────────────────────────────────────────────

@pytest.mark.parametrize("role", ["admin", "staff"])
async def test_admin_and_staff_get_403_on_every_users_route(env, role):
    operator = await make_operator()
    caller = await make_admin(operator, role=role)
    target = await make_admin(operator, role="staff")
    h = await headers(env, caller)
    c = env["client"]
    responses = [
        await c.get(USERS, headers=h),
        await c.post(USERS, headers=h, json=new_user_body()),
        await c.patch(f"{USERS}/{target.id}", headers=h, json={"role": "admin"}),
        await c.post(f"{USERS}/{target.id}/disable", headers=h, json={}),
        await c.post(f"{USERS}/{target.id}/enable", headers=h, json={}),
    ]
    assert [denied(r) for r in responses] == [True] * 5
    assert (await fresh(target)).role == "staff" and (await fresh(target)).is_active


# ── Create ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["admin", "staff"])
async def test_superadmin_creates_admin_and_staff(env, role):
    operator = await make_operator()
    su = await make_admin(operator)
    body = new_user_body(role)
    res = await env["client"].post(USERS, headers=await headers(env, su), json=body)
    assert res.status_code == 201, res.text
    out = res.json()
    assert (out["role"], out["email"], out["is_active"], out["must_complete_onboarding"]) == (role, body["email"], True, True)
    assert out["temp_password"] and out["temp_password_sms_sent"] is True
    assert (body["email"], out["temp_password"]) in env["texts"]
    async with async_session_factory() as db:
        row = (await db.execute(select(AdminUser).where(AdminUser.email == body["email"]))).scalar_one()
    assert (row.isp_operator_id, row.role, row.phone_verified) == (operator.id, role, False)
    [entry] = await audit_rows(row.id, "admin_user.create")
    assert (entry.actor_type, entry.actor_id, entry.isp_operator_id) == ("operator_user", su.id, operator.id)
    assert entry.detail == {"role": role}


@pytest.mark.parametrize("role", ["superadmin", "viewer", "owner", ""])
async def test_create_refuses_any_other_role(env, role):
    operator = await make_operator()
    su = await make_admin(operator)
    body = new_user_body(role)
    res = await env["client"].post(USERS, headers=await headers(env, su), json=body)
    assert res.status_code == 422
    async with async_session_factory() as db:
        assert (await db.execute(select(AdminUser).where(AdminUser.email == body["email"]))).scalar_one_or_none() is None


async def test_create_ignores_an_operator_id_in_the_body(env):
    mine, theirs = await make_operator(), await make_operator()
    su = await make_admin(mine)
    body = new_user_body("staff", isp_operator_id=str(theirs.id), operator_id=str(theirs.id))
    res = await env["client"].post(USERS, headers=await headers(env, su), json=body)
    assert res.status_code == 201, res.text
    async with async_session_factory() as db:
        row = (await db.execute(select(AdminUser).where(AdminUser.email == body["email"]))).scalar_one()
    assert row.isp_operator_id == mine.id


async def test_create_with_a_taken_email_is_409(env):
    operator = await make_operator()
    su = await make_admin(operator)
    other = await make_admin(await make_operator(), role="staff")
    res = await env["client"].post(USERS, headers=await headers(env, su), json=new_user_body() | {"email": other.email})
    assert res.status_code == 409


# ── Role changes ────────────────────────────────────────────────────────────

async def test_role_change_takes_effect_on_the_next_request_and_is_audited(env):
    operator = await make_operator()
    su = await make_admin(operator)
    user = await make_admin(operator, role="admin")
    user_h = await headers(env, user)
    assert (await env["client"].get("/admin/config-templates", headers=user_h)).status_code == 200

    res = await env["client"].patch(f"{USERS}/{user.id}", headers=await headers(env, su), json={"role": "staff"})
    assert res.status_code == 200 and res.json()["role"] == "staff"
    assert denied(await env["client"].get("/admin/config-templates", headers=user_h))
    [entry] = await audit_rows(user.id, "admin_user.role_change")
    assert entry.changes == {"role": {"from": "admin", "to": "staff"}} and entry.actor_id == su.id


async def test_role_change_to_the_same_role_writes_nothing(env):
    operator = await make_operator()
    su = await make_admin(operator)
    user = await make_admin(operator, role="staff")
    res = await env["client"].patch(f"{USERS}/{user.id}", headers=await headers(env, su), json={"role": "staff"})
    assert res.status_code == 200
    assert await audit_rows(user.id, "admin_user.role_change") == []


@pytest.mark.parametrize("role", ["superadmin", "viewer"])
async def test_promoting_to_anything_but_admin_or_staff_is_422(env, role):
    operator = await make_operator()
    su = await make_admin(operator)
    user = await make_admin(operator, role="staff")
    res = await env["client"].patch(f"{USERS}/{user.id}", headers=await headers(env, su), json={"role": role})
    assert res.status_code == 422
    assert (await fresh(user)).role == "staff"


# ── Guards: superadmins, self, other tenants ────────────────────────────────

async def test_another_superadmin_of_the_same_tenant_is_untouchable(env):
    operator = await make_operator()
    su = await make_admin(operator)
    other_su = await make_admin(operator)
    h = await headers(env, su)
    c = env["client"]
    for res in (
        await c.patch(f"{USERS}/{other_su.id}", headers=h, json={"role": "admin"}),
        await c.post(f"{USERS}/{other_su.id}/disable", headers=h, json={}),
        await c.post(f"{USERS}/{other_su.id}/enable", headers=h, json={}),
    ):
        assert res.status_code == 403 and res.json()["detail"] == users_routes.NOT_SUPERADMIN
    row = await fresh(other_su)
    assert (row.role, row.is_active) == ("superadmin", True)


async def test_a_superadmin_cannot_change_themselves(env):
    operator = await make_operator()
    su = await make_admin(operator)
    h = await headers(env, su)
    c = env["client"]
    for res in (
        await c.patch(f"{USERS}/{su.id}", headers=h, json={"role": "staff"}),
        await c.post(f"{USERS}/{su.id}/disable", headers=h, json={}),
        await c.post(f"{USERS}/{su.id}/enable", headers=h, json={}),
    ):
        assert res.status_code == 403 and res.json()["detail"] == users_routes.NOT_YOURSELF
    row = await fresh(su)
    assert (row.role, row.is_active) == ("superadmin", True)


async def test_another_operators_user_is_404_for_every_verb_and_never_listed(env):
    a, b = await make_operator(), await make_operator()
    su_a = await make_admin(a)
    staff_b = await make_admin(b, role="staff")
    disabled_b = await make_admin(b, role="staff", is_active=False, disabled_at=datetime.now(timezone.utc),
                                  disabled_by_type="operator_user")
    h = await headers(env, su_a)
    c = env["client"]
    results = [
        await c.patch(f"{USERS}/{staff_b.id}", headers=h, json={"role": "admin"}),
        await c.post(f"{USERS}/{staff_b.id}/disable", headers=h, json={}),
        await c.post(f"{USERS}/{disabled_b.id}/enable", headers=h, json={}),
        await c.patch(f"{USERS}/{uuid.uuid4()}", headers=h, json={"role": "admin"}),
    ]
    assert [r.status_code for r in results] == [404] * 4
    assert len({r.text for r in results}) == 1  # another tenant's user looks exactly like no user
    listed = {u["id"] for u in (await c.get(USERS, headers=h)).json()}
    assert str(staff_b.id) not in listed and str(su_a.id) in listed
    assert ((await fresh(staff_b)).role, (await fresh(staff_b)).is_active, (await fresh(disabled_b)).is_active) == ("staff", True, False)


# ── PIN ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("state,reason", [("none", "setup"), ("expired", "verify")])
async def test_every_write_needs_a_recent_pin(env, state, reason):
    operator = await make_operator()
    if state == "none":
        su = await make_admin(operator, pin=False)
    else:
        su = await make_admin(operator, pin_verified_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    user = await make_admin(operator, role="staff")
    h = await headers(env, su)
    c = env["client"]
    writes = [
        await c.post(USERS, headers=h, json=new_user_body()),
        await c.patch(f"{USERS}/{user.id}", headers=h, json={"role": "admin"}),
        await c.post(f"{USERS}/{user.id}/disable", headers=h, json={}),
        await c.post(f"{USERS}/{user.id}/enable", headers=h, json={}),
    ]
    assert [(r.status_code, r.headers.get(PIN_REQUIRED_HEADER)) for r in writes] == [(403, reason)] * 4
    assert (await c.get(USERS, headers=h)).status_code == 200  # reading the list needs no PIN
    assert ((await fresh(user)).role, (await fresh(user)).is_active) == ("staff", True)


# ── Disable / enable ────────────────────────────────────────────────────────

async def test_disable_ends_sessions_sends_nothing_and_is_audited_then_enable_restores(env):
    operator = await make_operator()
    su = await make_admin(operator)
    user = await make_admin(operator, role="admin")
    user_h = await headers(env, user)
    su_h = await headers(env, su)
    c = env["client"]
    notices_before = len(env["notices"])

    res = await c.post(f"{USERS}/{user.id}/disable", headers=su_h, json={"reason": "Left the team"})
    assert res.status_code == 200, res.text
    out = res.json()
    assert (out["is_active"], out["disabled_by"], out["disabled_reason"]) == (False, "operator", "Left the team")
    assert (await c.get("/auth/me", headers=user_h)).status_code == 401
    row = await fresh(user)
    assert (row.disabled_by_type, row.disabled_by_id) == ("operator_user", su.id)
    [entry] = await audit_rows(user.id, "admin_user.disable")
    assert (entry.actor_type, entry.actor_id, entry.detail) == ("operator_user", su.id, {"reason": "Left the team"})
    assert (await c.post(f"{USERS}/{user.id}/disable", headers=su_h, json={})).status_code == 409

    res = await c.post(f"{USERS}/{user.id}/enable", headers=su_h, json={})
    assert res.status_code == 200 and res.json()["is_active"] is True
    assert (await c.post("/auth/login", json={"email": user.email, "password": PASSWORD})).status_code == 200
    assert (await c.post(f"{USERS}/{user.id}/enable", headers=su_h, json={})).status_code == 409
    [entry] = await audit_rows(user.id, "admin_user.enable")
    assert entry.detail["was_disabled_by"] == "operator_user"
    assert len(env["notices"]) == notices_before  # no SMS/email for an operator's own actions


async def test_a_user_the_platform_disabled_stays_disabled(env):
    operator = await make_operator()
    su = await make_admin(operator)
    user = await make_admin(operator, role="staff")
    res = await env["client"].post(f"/platform/operators/{operator.id}/admins/{user.id}/disable",
                                   headers=env["owner_headers"], json={"reason": "Fraud review — internal"})
    assert res.status_code == 200, res.text
    h = await headers(env, su)
    res = await env["client"].post(f"{USERS}/{user.id}/enable", headers=h, json={})
    assert res.status_code == 403 and res.json()["detail"] == users_routes.PLATFORM_DISABLED
    assert (await fresh(user)).is_active is False
    [listed] = [u for u in (await env["client"].get(USERS, headers=h)).json() if u["id"] == str(user.id)]
    assert (listed["disabled_by"], listed["disabled_reason"]) == ("platform", None)  # the platform's reason stays internal


# ── Platform side ───────────────────────────────────────────────────────────

async def test_platform_disabling_the_last_active_superadmin_needs_confirm(env):
    operator = await make_operator()
    su = await make_admin(operator)
    path = f"/platform/operators/{operator.id}/admins/{su.id}/disable"
    res = await env["client"].post(path, headers=env["owner_headers"], json={"reason": "Testing the last-superadmin guard"})
    assert res.status_code == 409
    assert (await fresh(su)).is_active is True
    res = await env["client"].post(path, headers=env["owner_headers"], json={"reason": "Testing the last-superadmin guard", "confirm_last_superadmin": True})
    assert res.status_code == 200
    assert (await fresh(su)).is_active is False


async def test_platform_still_creates_superadmin_admin_and_staff(env):
    operator = await make_operator()
    for role in ("superadmin", "admin", "staff"):
        res = await env["client"].post(f"/platform/operators/{operator.id}/admins", headers=env["owner_headers"],
                                       json={"email": f"p-{uuid.uuid4().hex[:8]}@throwaway.test", "phone": "0244000999", "role": role})
        assert res.status_code == 201 and res.json()["role"] == role, res.text
