"""Platform-owner sign-out (POST /platform/auth/logout) against a REAL database.

Sign-out bumps token_version, so it must end every session on every device at
once, by access token or by refresh token, for an owner with setup pending
too, and it must change nothing for a token that is not a current
platform-owner session. Always 204.

Runs the FastAPI app in-process, like the other platform-owner flow suites.
Only runs against a disposable database:

    PLATFORM_OWNER_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("PLATFORM_OWNER_FLOW_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs PLATFORM_OWNER_FLOW_TEST_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from fastapi.routing import APIRoute

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import PlatformOwner
    from src.middleware.auth import get_authenticated_platform_owner
    from src.modules.auth.tokens import platform_owner_token_data
    from src.modules.platform import routes as platform_routes
    from src.utils.auth import (
        create_access_token,
        create_login_challenge_token,
        create_platform_owner_access_token,
        hash_password,
    )
    from platform_owner_session import create_ready_owner, owner_login

PASSWORD = "Owner-Passw0rd"
PHONE = "233244000556"
LOGOUT = "/platform/auth/logout"


@pytest_asyncio.fixture(loop_scope="module")
async def client(monkeypatch):
    async def no_limit(key, bucket, limit=10, window_seconds=60):
        return None

    monkeypatch.setattr(platform_routes, "enforce_rate_limit", no_limit)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as c:
        yield c


async def ready_owner() -> tuple[str, str]:
    """A set-up owner. Returns (email, code)."""
    email = f"logout-{uuid.uuid4().hex[:10]}@throwaway.test"
    code = await create_ready_owner(
        email, PASSWORD, phone=PHONE, security_question="first_school", security_answer="Accra Academy",
    )
    return email, code


async def sign_in(client, email: str, code: str) -> dict:
    """A real session: password, then the character challenge."""
    res = await owner_login(client, email, PASSWORD, code)
    assert res.status_code == 200, res.text
    return res.json()


async def owner_by_email(email: str) -> "PlatformOwner":
    from sqlalchemy import select

    async with async_session_factory() as db:
        return (await db.execute(select(PlatformOwner).where(PlatformOwner.email == email))).scalar_one()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def session_works(client, tokens: dict) -> tuple[int, int]:
    """(status of GET /platform/me with the access token, status of refresh)."""
    me = await client.get("/platform/me", headers=bearer(tokens["access_token"]))
    refreshed = await client.post("/platform/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    return me.status_code, refreshed.status_code


# ── Structural ────────────────────────────────────────────────────────────


def test_logout_does_not_resolve_an_owner_through_the_gated_dependencies():
    """It reads the token itself: behind get_authenticated_platform_owner it
    would be an owner route, and the setup gate's route walk would require the
    gate, which would lock a setup-pending owner out of signing out."""
    routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path == "/api/v1" + LOGOUT]
    assert [sorted(r.methods) for r in routes] == [["POST"]]

    def calls(dependant, seen):
        for dep in dependant.dependencies:
            seen.add(dep.call)
            calls(dep, seen)
        return seen

    assert get_authenticated_platform_owner not in calls(routes[0].dependant, set())


# ── Behavioural ───────────────────────────────────────────────────────────


async def test_sign_out_with_the_access_token_ends_the_session_at_once(client):
    email, code = await ready_owner()
    tokens = await sign_in(client, email, code)
    assert await session_works(client, tokens) == (200, 200)
    before = (await owner_by_email(email)).token_version

    res = await client.post(LOGOUT, headers=bearer(tokens["access_token"]))
    assert res.status_code == 204
    assert res.content == b""
    assert (await owner_by_email(email)).token_version == before + 1
    # Immediately, not at expiry: the same unexpired access token is refused,
    # and so is the refresh token from the same sign-in.
    assert await session_works(client, tokens) == (401, 401)


async def test_sign_out_ends_every_device(client):
    email, code = await ready_owner()
    phone = await sign_in(client, email, code)
    laptop = await sign_in(client, email, code)
    assert await session_works(client, laptop) == (200, 200)

    assert (await client.post(LOGOUT, headers=bearer(phone["access_token"]))).status_code == 204
    assert await session_works(client, phone) == (401, 401)
    assert await session_works(client, laptop) == (401, 401)
    # Signing in again afterwards works as normal.
    again = await sign_in(client, email, code)
    assert await session_works(client, again) == (200, 200)


async def test_sign_out_with_only_the_refresh_token(client):
    email, code = await ready_owner()
    tokens = await sign_in(client, email, code)
    before = (await owner_by_email(email)).token_version

    res = await client.post(LOGOUT, json={"refresh_token": tokens["refresh_token"]})
    assert res.status_code == 204
    assert (await owner_by_email(email)).token_version == before + 1
    assert await session_works(client, tokens) == (401, 401)


async def test_an_expired_access_token_falls_back_to_the_refresh_token(client):
    """A page left open past the access token's lifetime can still sign out."""
    email, code = await ready_owner()
    tokens = await sign_in(client, email, code)
    owner = await owner_by_email(email)
    expired = create_platform_owner_access_token(platform_owner_token_data(owner), expires_delta=timedelta(minutes=-1))
    assert (await client.get("/platform/me", headers=bearer(expired))).status_code == 401

    res = await client.post(LOGOUT, headers=bearer(expired), json={"refresh_token": tokens["refresh_token"]})
    assert res.status_code == 204
    assert (await owner_by_email(email)).token_version == owner.token_version + 1
    assert await session_works(client, tokens) == (401, 401)


async def test_a_replayed_sign_out_bumps_only_once(client):
    email, code = await ready_owner()
    tokens = await sign_in(client, email, code)
    before = (await owner_by_email(email)).token_version

    for _ in range(3):
        res = await client.post(
            LOGOUT, headers=bearer(tokens["access_token"]), json={"refresh_token": tokens["refresh_token"]}
        )
        assert res.status_code == 204
    assert (await owner_by_email(email)).token_version == before + 1
    # An old token's sign-out must not end a newer session either.
    fresh = await sign_in(client, email, code)
    assert (await client.post(LOGOUT, headers=bearer(tokens["access_token"]))).status_code == 204
    assert await session_works(client, fresh) == (200, 200)


async def test_tokens_that_are_not_a_current_owner_session_change_nothing(client):
    email, code = await ready_owner()
    tokens = await sign_in(client, email, code)
    owner = await owner_by_email(email)
    data = platform_owner_token_data(owner)
    challenge = create_login_challenge_token(owner_id=str(owner.id), token_version=owner.token_version, jti="x")
    admin_issued = create_access_token(dict(data, isp_operator_id=str(uuid.uuid4())))

    attempts = [
        {},  # no token at all
        {"headers": bearer("not-a-jwt")},
        {"json": {"refresh_token": "not-a-jwt"}},
        {"headers": {"Authorization": "Basic " + tokens["access_token"]}},
        # Wrong token type in each slot.
        {"headers": bearer(tokens["refresh_token"])},
        {"json": {"refresh_token": tokens["access_token"]}},
        # Other issuers carrying the owner's id and current version.
        {"headers": bearer(challenge)},
        {"headers": bearer(admin_issued)},
        # A session token without the version claim.
        {"headers": bearer(create_platform_owner_access_token({k: v for k, v in data.items() if k != "tv"}))},
        # A well-formed token for an owner that does not exist.
        {"headers": bearer(create_platform_owner_access_token(dict(data, sub=str(uuid.uuid4()))))},
        {"headers": bearer(create_platform_owner_access_token(dict(data, sub="not-a-uuid")))},
    ]
    for kwargs in attempts:
        res = await client.post(LOGOUT, **kwargs)
        assert res.status_code == 204, (kwargs, res.status_code, res.text)
    assert (await owner_by_email(email)).token_version == owner.token_version
    assert await session_works(client, tokens) == (200, 200)


async def test_an_inactive_owner_is_not_touched(client):
    email, code = await ready_owner()
    tokens = await sign_in(client, email, code)
    owner = await owner_by_email(email)
    async with async_session_factory() as db:
        row = await db.get(PlatformOwner, owner.id)
        row.is_active = False
        await db.commit()

    assert (await client.post(LOGOUT, headers=bearer(tokens["access_token"]))).status_code == 204
    assert (await owner_by_email(email)).token_version == owner.token_version


async def test_an_owner_with_setup_pending_can_sign_out(client):
    """setup.html's Sign out: the owner can reach nothing but /platform/setup."""
    email = f"logout-pending-{uuid.uuid4().hex[:10]}@throwaway.test"
    async with async_session_factory() as db:
        db.add(PlatformOwner(email=email, password_hash=hash_password(PASSWORD), name="Owner",
                             is_active=True, must_complete_security_setup=True))
        await db.commit()
    res = await client.post("/platform/auth/login", json={"email": email, "password": PASSWORD})
    assert res.status_code == 200 and "access_token" in res.json(), res.text
    tokens = res.json()
    status = await client.get("/platform/setup/status", headers=bearer(tokens["access_token"]))
    assert status.status_code == 200, status.text
    before = (await owner_by_email(email)).token_version

    assert (await client.post(LOGOUT, headers=bearer(tokens["access_token"]))).status_code == 204
    assert (await owner_by_email(email)).token_version == before + 1
    status = await client.get("/platform/setup/status", headers=bearer(tokens["access_token"]))
    assert status.status_code == 401
