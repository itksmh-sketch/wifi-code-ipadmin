"""Disabling sign-in (P2) against a REAL database: per admin and per operator,
resellers included, the platform routes, notices and audit rows.

Runs the FastAPI app in-process. Only runs against a disposable database at
migration 056 or later:

    LOGIN_DISABLE_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("LOGIN_DISABLE_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs LOGIN_DISABLE_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import func, select, text
    from sqlalchemy.exc import IntegrityError

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import AdminOtpCode, AdminUser, AuditLog, ISPOperator, PlatformOwner, Reseller
    from src.middleware import auth as auth_middleware
    from src.modules.admin_accounts import routes as account_routes
    from src.modules.audit import read as audit_read
    from src.modules.auth import routes as auth_routes
    from src.modules.auth.tokens import platform_owner_token_data
    from src.modules.platform import routes as platform_routes
    from src.modules.resellers import routes as reseller_routes
    from src.utils.auth import (
        create_password_reset_token,
        create_platform_owner_access_token,
        hash_password,
    )
    from src.utils.reseller_auth import hash_reseller_password

PASSWORD = "Str0ngPassword"
PHONE = "233244000777"
NEW_PASSWORD = "An0therStr0ngOne"


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    async def no_limit(*args, **kwargs):
        return None

    for module in (account_routes, auth_routes, auth_middleware, reseller_routes, platform_routes):
        monkeypatch.setattr(module, "enforce_rate_limit", no_limit, raising=False)

    notices: list[dict] = []

    async def capture_notice(**kwargs):
        notices.append(kwargs)

    monkeypatch.setattr(platform_routes.notify, "notify_admin_access_changed", capture_notice)

    async def no_temp_sms(*args, **kwargs):
        from src.modules.sms.types import SMSSendResult
        return SMSSendResult(success=True, provider_reference="test")

    monkeypatch.setattr(platform_routes, "send_temp_password_sms", no_temp_sms)
    from src.modules.admin_accounts import notifications as admin_notifications
    monkeypatch.setattr(admin_notifications, "send_temp_password_sms", no_temp_sms)

    # Records which hash every admin login verified against.
    checked: list[str] = []
    real_verify = auth_routes.verify_password

    def recording_verify(plain, hashed):
        checked.append(hashed)
        return real_verify(plain, hashed)

    monkeypatch.setattr(auth_routes, "verify_password", recording_verify)

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
        yield {"client": client, "owner": owner, "owner_headers": bearer(owner_token),
               "notices": notices, "checked": checked}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def make_operator() -> "ISPOperator":
    slug = f"dis-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Dis {slug}", slug=slug, contact_email=f"{slug}@throwaway.test",
                               contact_phone="233244000111", status="approved")
        db.add(operator)
        await db.commit()
        return operator


async def make_admin(operator, *, role="superadmin", **values) -> "AdminUser":
    async with async_session_factory() as db:
        state = dict(
            isp_operator_id=operator.id, email=f"adm-{uuid.uuid4().hex[:10]}@throwaway.test",
            password_hash=hash_password(PASSWORD), role=role, is_active=True, phone=PHONE, phone_verified=True,
            must_complete_onboarding=False, must_change_password=False, security_question="birth_town",
            security_answer_hash=hash_password("accra"),
        )
        state.update(values)
        admin = AdminUser(**state)
        db.add(admin)
        await db.commit()
        return admin


async def make_reseller(operator) -> "Reseller":
    async with async_session_factory() as db:
        reseller = Reseller(isp_operator_id=operator.id, name="Res", email=f"res-{uuid.uuid4().hex[:10]}@throwaway.test",
                            password_hash=hash_reseller_password(PASSWORD), role="reseller", is_active=True)
        db.add(reseller)
        await db.commit()
        return reseller


async def fresh(model, row_id):
    async with async_session_factory() as db:
        return await db.get(model, row_id)


async def sign_in(client, admin) -> dict:
    res = await client.post("/auth/login", json={"email": admin.email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()


async def session_status(client, tokens) -> tuple[int, int]:
    me = await client.get("/auth/onboarding/status", headers=bearer(tokens["access_token"]))
    refreshed = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    return me.status_code, refreshed.status_code


def admin_path(operator, admin, action):
    return f"/platform/operators/{operator.id}/admins/{admin.id}/{action}"


def operator_path(operator, action):
    return f"/platform/operators/{operator.id}/admin-logins/{action}"


async def post(env, path, body=None):
    return await env["client"].post(path, headers=env["owner_headers"], json=body if body is not None else {})


async def audit_rows(target_id, action):
    async with async_session_factory() as db:
        return (await db.execute(
            select(AuditLog).where(AuditLog.target_id == str(target_id), AuditLog.action == action)
        )).scalars().all()


async def unknown_email_reply(client):
    return await client.post("/auth/login", json={"email": f"ghost-{uuid.uuid4().hex}@throwaway.test", "password": PASSWORD})


# ── Per-admin disable ─────────────────────────────────────────────────────


async def test_disable_admin_ends_sessions_and_sign_in_looks_like_unknown_email(env):
    client = env["client"]
    operator = await make_operator()
    admin = await make_admin(operator)
    await make_admin(operator)  # a second superadmin, so no confirmation is needed
    tokens = await sign_in(client, admin)
    assert await session_status(client, tokens) == (200, 200)

    res = await post(env, admin_path(operator, admin, "disable"), {"reason": "Fraud review"})
    assert res.status_code == 200, res.text
    assert res.json()["is_active"] is False and res.json()["disabled_by_type"] == "platform_owner"

    # Live sessions: both access and refresh refused at once.
    assert await session_status(client, tokens) == (401, 401)

    # Sign-in: identical reply to an unknown email, on the dummy-hash path,
    # with no failure counted and no audit row.
    env["checked"].clear()
    disabled = await client.post("/auth/login", json={"email": admin.email, "password": PASSWORD})
    via_disabled = list(env["checked"])
    env["checked"].clear()
    unknown = await unknown_email_reply(client)
    assert disabled.status_code == unknown.status_code == 401
    assert disabled.content == unknown.content
    assert disabled.headers["content-type"] == unknown.headers["content-type"]
    assert via_disabled == list(env["checked"]) == [auth_routes._DUMMY_HASH]
    row = await fresh(AdminUser, admin.id)
    assert row.login_attempt_count == 0
    assert await audit_rows(admin.id, "auth.login_failed") == []


async def test_disable_admin_bumps_version_clears_pin_and_expires_codes_and_grants(env):
    client = env["client"]
    operator = await make_operator()
    admin = await make_admin(operator, pin_verified_until=datetime.now(timezone.utc) + timedelta(minutes=10))
    await make_admin(operator)
    async with async_session_factory() as db:
        db.add(AdminOtpCode(admin_user_id=admin.id, purpose="reset", phone=PHONE, code_hash="x",
                            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10)))
        await db.commit()
    grant = create_password_reset_token(admin_id=str(admin.id), token_version=0, via="otp")

    assert (await post(env, admin_path(operator, admin, "disable"), {"reason": "Leaver"})).status_code == 200

    row = await fresh(AdminUser, admin.id)
    assert row.token_version == 1 and row.pin_verified_until is None
    assert (row.disabled_reason, row.disabled_by_id) == ("Leaver", env["owner"].id)
    async with async_session_factory() as db:
        open_codes = (await db.execute(select(func.count()).select_from(AdminOtpCode).where(
            AdminOtpCode.admin_user_id == admin.id, AdminOtpCode.consumed_at.is_(None)))).scalar_one()
    assert open_codes == 0
    res = await client.post("/auth/reset/set-password",
                            json={"reset_token": grant, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    assert res.status_code == 400
    # Forgot-password can't be started for it either: it is not a resettable account.
    assert (await fresh(AdminUser, admin.id)).password_hash == row.password_hash


async def test_re_enable_clears_flags_only_and_old_tokens_stay_dead(env):
    client = env["client"]
    operator = await make_operator()
    admin = await make_admin(operator)
    await make_admin(operator)
    tokens = await sign_in(client, admin)
    await post(env, admin_path(operator, admin, "disable"), {"reason": "Pause"})

    res = await post(env, admin_path(operator, admin, "enable"))
    assert res.status_code == 200 and res.json()["is_active"] is True
    row = await fresh(AdminUser, admin.id)
    assert (row.disabled_at, row.disabled_by_type, row.disabled_by_id, row.disabled_reason) == (None,) * 4
    assert row.token_version == 1  # unchanged by the enable
    assert await session_status(client, tokens) == (401, 401)
    assert await session_status(client, await sign_in(client, admin)) == (200, 200)


async def test_double_disable_and_enable_of_active_are_conflicts(env):
    operator = await make_operator()
    admin = await make_admin(operator)
    await make_admin(operator)
    assert (await post(env, admin_path(operator, admin, "enable"))).status_code == 409
    assert (await post(env, admin_path(operator, admin, "disable"), {"reason": "x1x"})).status_code == 200
    assert (await post(env, admin_path(operator, admin, "disable"), {"reason": "x1x"})).status_code == 409


async def test_last_active_superadmin_needs_confirmation(env):
    operator = await make_operator()
    only = await make_admin(operator)
    await make_admin(operator, role="admin")
    res = await post(env, admin_path(operator, only, "disable"), {"reason": "Check"})
    assert res.status_code == 409 and "last active superadmin" in res.json()["detail"]
    assert (await fresh(AdminUser, only.id)).is_active is True
    res = await post(env, admin_path(operator, only, "disable"), {"reason": "Check", "confirm_last_superadmin": True})
    assert res.status_code == 200


async def test_admin_of_another_operator_is_not_found(env):
    mine, theirs = await make_operator(), await make_operator()
    their_admin = await make_admin(theirs)
    for action, body in (("disable", {"reason": "nope"}), ("enable", {})):
        res = await post(env, admin_path(mine, their_admin, action), body)
        assert res.status_code == 404
    assert (await fresh(AdminUser, their_admin.id)).is_active is True


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "  a "}, {"reason": "ok reason", "extra": 1},
                                  {"reason": "x" * 501}])
async def test_disable_needs_a_real_reason_and_nothing_else(env, body):
    operator = await make_operator()
    admin = await make_admin(operator)
    await make_admin(operator)
    assert (await post(env, admin_path(operator, admin, "disable"), body)).status_code == 422
    assert (await post(env, operator_path(operator, "disable"), body)).status_code == 422


async def test_routes_need_a_platform_owner(env):
    client = env["client"]
    operator = await make_operator()
    admin = await make_admin(operator)
    admin_tokens = await sign_in(client, admin)
    for path in (operator_path(operator, "disable"), admin_path(operator, admin, "disable")):
        assert (await client.post(path, json={"reason": "abc"})).status_code in (401, 403)
        assert (await client.post(path, json={"reason": "abc"}, headers=bearer(admin_tokens["access_token"]))).status_code == 401
    assert (await fresh(ISPOperator, operator.id)).admin_logins_disabled_at is None


# ── Operator-wide switch ──────────────────────────────────────────────────


async def test_operator_disable_signs_everyone_out_in_one_go(env):
    client = env["client"]
    operator = await make_operator()
    first = await make_admin(operator, pin_verified_until=datetime.now(timezone.utc) + timedelta(minutes=10))
    second = await make_admin(operator, role="admin")
    bystander = await make_admin(await make_operator())
    sessions = [await sign_in(client, a) for a in (first, second)]
    bystander_tokens = await sign_in(client, bystander)

    res = await post(env, operator_path(operator, "disable"), {"reason": "Chargeback investigation"})
    assert res.status_code == 200, res.text
    assert res.json()["admins_signed_out"] == 2 and res.json()["admin_logins_disabled_at"]

    for tokens in sessions:
        assert await session_status(client, tokens) == (401, 401)
    for admin in (first, second):
        row = await fresh(AdminUser, admin.id)
        assert row.token_version == 1 and row.pin_verified_until is None and row.is_active is True
    # Another tenant is untouched.
    assert await session_status(client, bystander_tokens) == (200, 200)


async def test_operator_disabled_sign_in_is_identical_to_unknown_email(env):
    """The body/timing identity check: same bytes, same headers, same
    single verify against the dummy hash, no counter moved, no audit row."""
    client = env["client"]
    operator = await make_operator()
    admin = await make_admin(operator)
    await post(env, operator_path(operator, "disable"), {"reason": "Investigation"})

    env["checked"].clear()
    blocked = await client.post("/auth/login", json={"email": admin.email, "password": PASSWORD})
    via_blocked = list(env["checked"])
    env["checked"].clear()
    unknown = await unknown_email_reply(client)
    assert blocked.status_code == unknown.status_code == 401
    assert blocked.content == unknown.content
    # Every header but the clock.
    assert {k: v for k, v in blocked.headers.items() if k != "date"} == {k: v for k, v in unknown.headers.items() if k != "date"}
    assert via_blocked == list(env["checked"]) == [auth_routes._DUMMY_HASH]
    assert (await fresh(AdminUser, admin.id)).login_attempt_count == 0
    assert await audit_rows(admin.id, "auth.login_failed") == []


async def test_operator_disable_blocks_reset_flows(env):
    client = env["client"]
    operator = await make_operator()
    admin = await make_admin(operator)
    grant = create_password_reset_token(admin_id=str(admin.id), token_version=0, via="otp")
    await post(env, operator_path(operator, "disable"), {"reason": "Investigation"})
    res = await client.post("/auth/reset/set-password",
                            json={"reset_token": grant, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    assert res.status_code == 400
    # reset/request answers the same as for any non-resettable address.
    sent = []
    original = account_routes._send_reset_code_in_background

    async def capture(phone, code):
        sent.append(phone)

    account_routes._send_reset_code_in_background = capture
    try:
        res = await client.post("/auth/reset/request", json={"email": admin.email})
    finally:
        account_routes._send_reset_code_in_background = original
    assert res.status_code == 200 and sent == []


async def test_user_created_while_disabled_is_blocked_too(env):
    client = env["client"]
    operator = await make_operator()
    await make_admin(operator)
    await post(env, operator_path(operator, "disable"), {"reason": "Investigation"})
    res = await env["client"].post(f"/platform/operators/{operator.id}/admins", headers=env["owner_headers"],
                                   json={"email": f"new-{uuid.uuid4().hex[:8]}@throwaway.test", "phone": "0244000999",
                                         "role": "admin"})
    assert res.status_code == 201, res.text
    created = res.json()
    login = await client.post("/auth/login", json={"email": created["email"], "password": created["temp_password"]})
    assert login.status_code == 401
    await post(env, operator_path(operator, "enable"))
    login = await client.post("/auth/login", json={"email": created["email"], "password": created["temp_password"]})
    assert login.status_code == 200


async def test_operator_enable_does_not_revive_individually_disabled_admins(env):
    client = env["client"]
    operator = await make_operator()
    kept_off = await make_admin(operator, role="admin")
    other = await make_admin(operator)
    old_tokens = await sign_in(client, other)
    await post(env, admin_path(operator, kept_off, "disable"), {"reason": "Leaver"})
    await post(env, operator_path(operator, "disable"), {"reason": "Investigation"})

    res = await post(env, operator_path(operator, "enable"), {"reason": "Cleared"})
    assert res.status_code == 200 and res.json()["admin_logins_disabled_at"] is None
    op = await fresh(ISPOperator, operator.id)
    assert (op.admin_logins_disabled_by, op.admin_logins_disabled_reason) == (None, None)

    assert (await client.post("/auth/login", json={"email": kept_off.email, "password": PASSWORD})).status_code == 401
    assert (await fresh(AdminUser, kept_off.id)).disabled_reason == "Leaver"
    assert await session_status(client, old_tokens) == (401, 401)  # sessions ended by the disable stay ended
    assert await session_status(client, await sign_in(client, other)) == (200, 200)


async def test_operator_switch_conflicts(env):
    operator = await make_operator()
    assert (await post(env, operator_path(operator, "enable"))).status_code == 409
    assert (await post(env, operator_path(operator, "disable"), {"reason": "abc"})).status_code == 200
    assert (await post(env, operator_path(operator, "disable"), {"reason": "abc"})).status_code == 409
    assert (await post(env, f"/platform/operators/{uuid.uuid4()}/admin-logins/disable", {"reason": "abc"})).status_code == 404


async def test_logout_while_operator_disabled_is_a_quiet_no_op(env):
    client = env["client"]
    operator = await make_operator()
    admin = await make_admin(operator)
    tokens = await sign_in(client, admin)
    await post(env, operator_path(operator, "disable"), {"reason": "Investigation"})
    res = await client.post("/auth/logout", headers=bearer(tokens["access_token"]))
    assert res.status_code == 204
    assert (await fresh(AdminUser, admin.id)).token_version == 1  # the disable's bump only
    assert await audit_rows(admin.id, "auth.logout") == []


# ── Resellers ─────────────────────────────────────────────────────────────


async def test_resellers_are_blocked_while_operator_logins_are_disabled(env):
    client = env["client"]
    operator = await make_operator()
    reseller = await make_reseller(operator)
    login = await client.post("/reseller/auth/login", json={"email": reseller.email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    tokens = login.json()
    assert (await client.get("/reseller/me", headers=bearer(tokens["access_token"]))).status_code == 200

    await post(env, operator_path(operator, "disable"), {"reason": "Investigation"})
    assert (await client.get("/reseller/me", headers=bearer(tokens["access_token"]))).status_code == 401
    assert (await client.post("/reseller/auth/refresh", json={"refresh_token": tokens["refresh_token"]})).status_code == 401
    blocked = await client.post("/reseller/auth/login", json={"email": reseller.email, "password": PASSWORD})
    unknown = await client.post("/reseller/auth/login", json={"email": f"ghost-{uuid.uuid4().hex}@throwaway.test", "password": PASSWORD})
    assert blocked.status_code == unknown.status_code == 401 and blocked.content == unknown.content

    await post(env, operator_path(operator, "enable"))
    assert (await client.get("/reseller/me", headers=bearer(tokens["access_token"]))).status_code == 200
    assert (await client.post("/reseller/auth/login", json={"email": reseller.email, "password": PASSWORD})).status_code == 200


async def test_per_admin_disable_does_not_touch_resellers(env):
    client = env["client"]
    operator = await make_operator()
    admin = await make_admin(operator)
    await make_admin(operator)
    reseller = await make_reseller(operator)
    await post(env, admin_path(operator, admin, "disable"), {"reason": "Leaver"})
    assert (await client.post("/reseller/auth/login", json={"email": reseller.email, "password": PASSWORD})).status_code == 200


# ── Platform password reset ───────────────────────────────────────────────


async def test_platform_reset_refuses_a_disabled_admin(env):
    operator = await make_operator()
    admin = await make_admin(operator)
    await make_admin(operator)
    await post(env, admin_path(operator, admin, "disable"), {"reason": "Leaver"})
    res = await post(env, f"/platform/operators/{operator.id}/admins/{admin.id}/reset-password")
    assert res.status_code == 409 and "disabled" in res.json()["detail"]
    assert (await fresh(AdminUser, admin.id)).must_change_password is False


async def test_platform_reset_refuses_while_operator_logins_are_disabled(env):
    operator = await make_operator()
    admin = await make_admin(operator)
    await post(env, operator_path(operator, "disable"), {"reason": "Investigation"})
    res = await post(env, f"/platform/operators/{operator.id}/admins/{admin.id}/reset-password")
    assert res.status_code == 409 and "operator" in res.json()["detail"]
    await post(env, operator_path(operator, "enable"))
    assert (await post(env, f"/platform/operators/{operator.id}/admins/{admin.id}/reset-password")).status_code == 200


# ── Notices and audit ─────────────────────────────────────────────────────


async def test_platform_actions_notify_without_the_reason(env):
    operator = await make_operator()
    admin = await make_admin(operator)
    await make_admin(operator)
    env["notices"].clear()
    await post(env, admin_path(operator, admin, "disable"), {"reason": "Secret internal reason"})
    await post(env, admin_path(operator, admin, "enable"))
    await post(env, operator_path(operator, "disable"), {"reason": "Secret internal reason"})
    await post(env, operator_path(operator, "enable"))
    assert [(n["disabled"], n["access_scope"]) for n in env["notices"]] == [
        (True, admin.email), (False, admin.email),
        (True, platform_routes.ALL_USERS_SCOPE), (False, platform_routes.ALL_USERS_SCOPE),
    ]
    assert env["notices"][0]["phone"] == PHONE and env["notices"][2]["email"] == operator.contact_email
    assert "Secret internal reason" not in repr(env["notices"])


async def test_unverified_phone_gets_no_sms(env):
    operator = await make_operator()
    admin = await make_admin(operator, phone_verified=False)
    await make_admin(operator)
    env["notices"].clear()
    await post(env, admin_path(operator, admin, "disable"), {"reason": "Leaver"})
    assert env["notices"][0]["phone"] is None and env["notices"][0]["email"] == admin.email


async def test_refused_actions_neither_notify_nor_audit(env):
    operator = await make_operator()
    only = await make_admin(operator)
    env["notices"].clear()
    await post(env, admin_path(operator, only, "disable"), {"reason": "Check"})  # 409: last superadmin
    await post(env, operator_path(operator, "enable"))  # 409: not disabled
    assert env["notices"] == []
    assert await audit_rows(only.id, "admin_user.disable") == []
    assert await audit_rows(operator.id, "operator.admin_logins_enable") == []


async def test_actions_are_audited_as_the_platform_owner(env):
    operator = await make_operator()
    admin = await make_admin(operator)
    await make_admin(operator)
    await post(env, admin_path(operator, admin, "disable"), {"reason": "Leaver"})
    await post(env, admin_path(operator, admin, "enable"), {"reason": "Back"})
    await post(env, operator_path(operator, "disable"), {"reason": "Investigation"})
    await post(env, operator_path(operator, "enable"))

    rows = (await audit_rows(admin.id, "admin_user.disable") + await audit_rows(admin.id, "admin_user.enable")
            + await audit_rows(operator.id, "operator.admin_logins_disable")
            + await audit_rows(operator.id, "operator.admin_logins_enable"))
    assert len(rows) == 4
    for row in rows:
        assert (row.actor_type, row.actor_id, row.actor_label) == ("platform_owner", env["owner"].id, env["owner"].email)
        assert row.isp_operator_id == operator.id and row.changes is None
        # The operator is shown "Platform", nothing that identifies the owner.
        shown = audit_read.present_actor(row.actor_type, row.actor_id, row.actor_label, viewer=audit_read.VIEWER_OPERATOR)
        assert shown == {"type": "platform_owner", "id": None, "label": "Platform"}
    assert rows[0].detail == {"reason": "Leaver"}
    assert rows[1].detail == {"reason": "Back", "was_disabled_by": "platform_owner"}
    assert rows[2].detail["reason"] == "Investigation" and rows[2].detail["admins_signed_out"] == 2
    assert rows[3].detail is None or rows[3].detail == {"reason": None}


# ── Database rules ────────────────────────────────────────────────────────


async def test_disabled_state_check_constraint(env):
    operator = await make_operator()
    admin = await make_admin(operator)
    async with async_session_factory() as db:
        with pytest.raises(IntegrityError, match="ck_admin_users_disabled_state"):
            await db.execute(text("UPDATE admin_users SET is_active = false WHERE id = :id"), {"id": admin.id})
    async with async_session_factory() as db:
        with pytest.raises(IntegrityError, match="ck_admin_users_disabled_state"):
            await db.execute(text("UPDATE admin_users SET disabled_reason = 'x' WHERE id = :id"), {"id": admin.id})
    async with async_session_factory() as db:
        with pytest.raises(IntegrityError, match="ck_isp_operators_admin_logins_disabled"):
            await db.execute(text("UPDATE isp_operators SET admin_logins_disabled_reason = 'x' WHERE id = :id"),
                             {"id": operator.id})
    async with async_session_factory() as db:
        with pytest.raises(IntegrityError):
            await db.execute(text("UPDATE admin_users SET is_active = NULL WHERE id = :id"), {"id": admin.id})


async def test_seeded_templates_exist(env):
    async with async_session_factory() as db:
        rows = (await db.execute(text(
            "SELECT event_type, channel FROM platform_notification_templates "
            "WHERE event_type IN ('admin_access_disabled', 'admin_access_restored') ORDER BY 1, 2"
        ))).all()
    assert [tuple(r) for r in rows] == [
        ("admin_access_disabled", "email"), ("admin_access_disabled", "sms"),
        ("admin_access_restored", "email"), ("admin_access_restored", "sms"),
    ]


async def test_audit_reads_treat_json_null_as_null(env):
    marker = f"test.jsonnull.{uuid.uuid4().hex[:8]}"
    async with async_session_factory() as db:
        await db.execute(text(
            "INSERT INTO audit_log (actor_type, action, changes, detail) "
            "VALUES ('system', :a, 'null'::jsonb, 'null'::jsonb)"), {"a": marker})
        await db.commit()
        raw = (await db.execute(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == marker, AuditLog.changes.is_(None)))).scalar_one()
        safe = (await db.execute(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == marker, audit_read.null_safe(AuditLog.changes).is_(None),
            audit_read.null_safe(AuditLog.detail).is_(None)))).scalar_one()
        selected = (await db.execute(select(audit_read.changes_column(), audit_read.detail_column())
                                     .where(AuditLog.action == marker))).one()
    assert (raw, safe) == (0, 1)
    assert tuple(selected) == (None, None)
