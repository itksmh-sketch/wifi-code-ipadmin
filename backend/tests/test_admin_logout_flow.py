"""Operator-admin sign-out (POST /auth/logout), and sign-in resetting the PIN
elevation, against a REAL database.

Sign-out bumps the admin's token_version, so it must end every session that
admin has on every device at once (and none of their colleagues'), clear the
PIN elevation stored on the account, record one audit event, and work whatever
state the account is in: mid-onboarding, suspended operator, login locked.
Anything that is not a current session of an active admin changes nothing.
Always 204.

Runs the FastAPI app in-process, like tests/test_admin_security_flow.py. Only
runs against a disposable database:

    ADMIN_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

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
    from fastapi.routing import APIRoute
    from sqlalchemy import select

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import AdminSecurityEvent, AdminUser, ISPOperator
    from src.middleware import auth as auth_middleware
    from src.middleware.auth import get_authenticated_admin, get_current_user
    from src.modules.admin_accounts import lockout
    from src.modules.admin_accounts import routes as account_routes
    from src.modules.auth import routes as auth_routes
    from src.modules.auth.tokens import admin_token_data
    from src.utils.auth import (
        create_access_token,
        create_password_reset_token,
        create_platform_owner_access_token,
        hash_password,
    )
    from src.utils.reseller_auth import create_reseller_access_token

PASSWORD = "Str0ngPassword"
PIN = "4729"
PHONE = "233244000777"
LOGOUT = "/auth/logout"
# Gated by require_recent_pin; the body is valid, so past the gate it succeeds.
PIN_GATED = ("POST", "/auth/me/phone", {"phone": PHONE})


@pytest_asyncio.fixture(loop_scope="module")
async def client(monkeypatch):
    # No Redis on this network: the limiter would fail open anyway, after a
    # connection timeout on every request.
    async def no_limit(key, bucket, limit=10, window_seconds=60):
        return None

    for module in (account_routes, auth_routes, auth_middleware):
        monkeypatch.setattr(module, "enforce_rate_limit", no_limit)

    async def no_sms(*args, **kwargs):
        from src.modules.sms.types import SMSSendResult

        return SMSSendResult(success=True, provider_reference="test")

    monkeypatch.setattr(account_routes, "send_otp_sms", no_sms)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as c:
        yield c


async def make_operator(status: str = "approved") -> "uuid.UUID":
    slug = f"out-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Out {slug}", slug=slug, contact_email=f"{slug}@throwaway.test", status=status)
        db.add(operator)
        await db.commit()
        return operator.id


async def make_admin(operator_id=None, *, onboarded: bool = True, pin: str | None = None, **values) -> str:
    """An admin (on a fresh operator unless one is given). Returns the email."""
    operator_id = operator_id or await make_operator()
    email = f"out-{uuid.uuid4().hex[:10]}@throwaway.test"
    state = (
        dict(phone=PHONE, phone_verified=True, must_complete_onboarding=False, must_change_password=False,
             security_question="birth_town", security_answer_hash=hash_password("accra"))
        if onboarded else
        # A brand-new admin on a temporary password: nothing set up yet.
        dict(phone=PHONE, phone_verified=False, must_complete_onboarding=True, must_change_password=True,
             security_question=None, security_answer_hash=None)
    )
    state.update(values)
    async with async_session_factory() as db:
        db.add(AdminUser(
            isp_operator_id=operator_id, email=email, password_hash=hash_password(PASSWORD), role="superadmin",
            is_active=True, pin_hash=hash_password(pin) if pin else None,
            pin_set_at=datetime.now(timezone.utc) if pin else None, **state,
        ))
        await db.commit()
    return email


async def row(email) -> "AdminUser":
    async with async_session_factory() as db:
        return (await db.execute(select(AdminUser).where(AdminUser.email == email))).scalar_one()


async def patch_admin(email, **values):
    async with async_session_factory() as db:
        admin = (await db.execute(select(AdminUser).where(AdminUser.email == email))).scalar_one()
        for key, value in values.items():
            setattr(admin, key, value)
        await db.commit()


async def signed_out_events(email):
    admin = await row(email)
    async with async_session_factory() as db:
        return (await db.execute(
            select(AdminSecurityEvent).where(
                AdminSecurityEvent.admin_user_id == admin.id, AdminSecurityEvent.event_type == "signed_out"
            )
        )).scalars().all()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def sign_in(client, email) -> dict:
    res = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()


async def session_works(client, tokens) -> tuple[int, int]:
    """(status of an ungated authenticated read, status of refresh).
    /auth/onboarding/status needs only a valid, current token, so it answers
    200 for every account state the tests use."""
    me = await client.get("/auth/onboarding/status", headers=bearer(tokens["access_token"]))
    refreshed = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    return me.status_code, refreshed.status_code


async def verify_pin(client, token):
    res = await client.post("/auth/me/pin/verify", headers=bearer(token), json={"pin": PIN})
    assert res.status_code == 200, res.text


async def pin_gated(client, token):
    method, path, body = PIN_GATED
    return await client.request(method, path, headers=bearer(token), json=body)


# ── Structural ────────────────────────────────────────────────────────────


def test_logout_sits_behind_no_admin_auth_dependency():
    """Behind get_authenticated_admin it would 401 a stale page; behind
    get_current_user it would 403 an admin mid-onboarding."""
    routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path == "/api/v1" + LOGOUT]
    assert [sorted(r.methods) for r in routes] == [["POST"]]

    def calls(dependant, seen):
        for dep in dependant.dependencies:
            seen.add(dep.call)
            calls(dep, seen)
        return seen

    found = calls(routes[0].dependant, set())
    assert get_authenticated_admin not in found and get_current_user not in found


# ── Ends this admin's sessions, all of them, at once ──────────────────────


async def test_sign_out_with_the_access_token_ends_the_session_at_once(client):
    email = await make_admin()
    tokens = await sign_in(client, email)
    assert await session_works(client, tokens) == (200, 200)
    before = (await row(email)).token_version

    res = await client.post(LOGOUT, headers=bearer(tokens["access_token"]))
    assert res.status_code == 204 and res.content == b""
    assert (await row(email)).token_version == before + 1
    assert await session_works(client, tokens) == (401, 401)


async def test_sign_out_ends_every_device_but_no_colleague(client):
    operator_id = await make_operator()
    email = await make_admin(operator_id)
    colleague = await make_admin(operator_id)
    phone, laptop = await sign_in(client, email), await sign_in(client, email)
    theirs = await sign_in(client, colleague)

    assert (await client.post(LOGOUT, headers=bearer(phone["access_token"]))).status_code == 204
    assert await session_works(client, phone) == (401, 401)
    assert await session_works(client, laptop) == (401, 401)
    assert await session_works(client, theirs) == (200, 200)
    # Signing in again afterwards works as normal.
    assert await session_works(client, await sign_in(client, email)) == (200, 200)


async def test_sign_out_with_only_the_refresh_token(client):
    email = await make_admin()
    tokens = await sign_in(client, email)
    before = (await row(email)).token_version

    assert (await client.post(LOGOUT, json={"refresh_token": tokens["refresh_token"]})).status_code == 204
    assert (await row(email)).token_version == before + 1
    assert await session_works(client, tokens) == (401, 401)


async def test_an_expired_access_token_falls_back_to_the_refresh_token(client):
    email = await make_admin()
    tokens = await sign_in(client, email)
    admin = await row(email)
    expired = create_access_token(admin_token_data(admin), expires_delta=timedelta(minutes=-1))
    assert (await client.get("/auth/onboarding/status", headers=bearer(expired))).status_code == 401

    res = await client.post(LOGOUT, headers=bearer(expired), json={"refresh_token": tokens["refresh_token"]})
    assert res.status_code == 204
    assert (await row(email)).token_version == admin.token_version + 1


async def test_a_replayed_sign_out_bumps_once_and_records_one_event(client):
    email = await make_admin()
    tokens = await sign_in(client, email)
    before = (await row(email)).token_version

    for _ in range(3):
        res = await client.post(
            LOGOUT, headers=bearer(tokens["access_token"]), json={"refresh_token": tokens["refresh_token"]}
        )
        assert res.status_code == 204
    assert (await row(email)).token_version == before + 1
    events = await signed_out_events(email)
    assert len(events) == 1
    assert events[0].detail["via"] == "access"
    assert events[0].isp_operator_id == (await row(email)).isp_operator_id
    # An old token's sign-out must not end a newer session.
    fresh = await sign_in(client, email)
    assert (await client.post(LOGOUT, headers=bearer(tokens["access_token"]))).status_code == 204
    assert await session_works(client, fresh) == (200, 200)


async def test_tokens_that_are_not_a_current_admin_session_change_nothing(client):
    email = await make_admin()
    tokens = await sign_in(client, email)
    admin = await row(email)
    data = admin_token_data(admin)
    other_operator = str(uuid.uuid4())

    attempts = [
        {},  # no token at all
        {"headers": bearer("not-a-jwt")},
        {"json": {"refresh_token": "not-a-jwt"}},
        {"headers": {"Authorization": "Basic " + tokens["access_token"]}},
        # Wrong token type in each slot.
        {"headers": bearer(tokens["refresh_token"])},
        {"json": {"refresh_token": tokens["access_token"]}},
        # Other issuers carrying this admin's id and current version.
        {"headers": bearer(create_platform_owner_access_token(data))},
        {"headers": bearer(create_reseller_access_token(data))},
        {"headers": bearer(create_password_reset_token(admin_id=str(admin.id), token_version=admin.token_version, via="otp"))},
        # Session-shaped tokens that do not match this admin.
        {"headers": bearer(create_access_token({k: v for k, v in data.items() if k != "tv"}))},
        {"headers": bearer(create_access_token(dict(data, isp_operator_id=other_operator)))},
        {"headers": bearer(create_access_token(dict(data, sub=str(uuid.uuid4()))))},
        {"headers": bearer(create_access_token(dict(data, sub="not-a-uuid")))},
        {"headers": bearer(create_access_token(dict(data, tv=admin.token_version + 1)))},
    ]
    for kwargs in attempts:
        res = await client.post(LOGOUT, **kwargs)
        assert res.status_code == 204, (kwargs, res.status_code, res.text)
    assert (await row(email)).token_version == admin.token_version
    assert await signed_out_events(email) == []
    assert await session_works(client, tokens) == (200, 200)


async def test_an_inactive_admin_is_not_touched(client):
    email = await make_admin()
    tokens = await sign_in(client, email)
    before = (await row(email)).token_version
    await patch_admin(email, is_active=False)

    assert (await client.post(LOGOUT, headers=bearer(tokens["access_token"]))).status_code == 204
    assert (await row(email)).token_version == before
    assert await signed_out_events(email) == []


# ── No gate in front of it ────────────────────────────────────────────────


async def test_an_admin_mid_onboarding_can_sign_out(client):
    """Temp password, phone not verified, no security question."""
    email = await make_admin(onboarded=False)
    tokens = await sign_in(client, email)
    # The account really is gated: the dashboard API refuses it.
    gated = await client.get("/auth/me/security", headers=bearer(tokens["access_token"]))
    assert gated.status_code == 403 and gated.headers.get("X-Onboarding-Required") == "1"
    before = (await row(email)).token_version

    assert (await client.post(LOGOUT, headers=bearer(tokens["access_token"]))).status_code == 204
    assert (await row(email)).token_version == before + 1
    assert await session_works(client, tokens) == (401, 401)


async def test_an_admin_of_a_suspended_operator_can_sign_out(client):
    email = await make_admin(await make_operator(status="suspended"))
    tokens = await sign_in(client, email)
    before = (await row(email)).token_version

    assert (await client.post(LOGOUT, headers=bearer(tokens["access_token"]))).status_code == 204
    assert (await row(email)).token_version == before + 1
    assert await session_works(client, tokens) == (401, 401)


async def test_a_live_session_of_a_login_locked_account_can_sign_out(client):
    """A login lock stops new sign-ins; it does not end live sessions, so one
    of those must still be able to sign out."""
    email = await make_admin()
    tokens = await sign_in(client, email)
    await patch_admin(
        email,
        login_attempt_count=lockout.LOGIN_MAX_ATTEMPTS,
        login_locked_until=datetime.now(timezone.utc) + timedelta(hours=3),
    )
    assert lockout.is_locked(await row(email), lockout.LOGIN)
    assert await session_works(client, tokens) == (200, 200)
    before = (await row(email)).token_version

    assert (await client.post(LOGOUT, headers=bearer(tokens["access_token"]))).status_code == 204
    assert (await row(email)).token_version == before + 1
    assert await session_works(client, tokens) == (401, 401)


# ── PIN elevation ─────────────────────────────────────────────────────────


async def test_sign_out_clears_the_pin_elevation(client):
    email = await make_admin(pin=PIN)
    tokens = await sign_in(client, email)
    await verify_pin(client, tokens["access_token"])
    assert (await row(email)).pin_verified_until is not None

    assert (await client.post(LOGOUT, headers=bearer(tokens["access_token"]))).status_code == 204
    assert (await row(email)).pin_verified_until is None


async def test_a_new_sign_in_does_not_inherit_someone_elses_pin_window(client):
    """The real admin enters the PIN; someone with only the password signs in
    inside the 15 minutes. That new session must be refused the PIN-gated
    areas until the PIN is entered on it."""
    email = await make_admin(pin=PIN)
    real = await sign_in(client, email)
    await verify_pin(client, real["access_token"])
    assert (await pin_gated(client, real["access_token"])).status_code == 200

    intruder = await sign_in(client, email)
    assert (await row(email)).pin_verified_until is None
    res = await pin_gated(client, intruder["access_token"])
    assert res.status_code == 403
    assert res.headers["X-Pin-Required"] == "verify"

    # Entering the PIN on the new session is what opens it.
    await verify_pin(client, intruder["access_token"])
    assert (await pin_gated(client, intruder["access_token"])).status_code == 200


async def test_a_failed_sign_in_leaves_the_pin_window_alone(client):
    """Only a successful sign-in resets it, so a stranger typing a wrong
    password can't knock the real admin out of their window."""
    email = await make_admin(pin=PIN)
    real = await sign_in(client, email)
    await verify_pin(client, real["access_token"])

    res = await client.post("/auth/login", json={"email": email, "password": "Wrong-Passw0rd"})
    assert res.status_code == 401
    assert (await row(email)).pin_verified_until is not None
    assert (await pin_gated(client, real["access_token"])).status_code == 200
