"""The platform-owner security-setup gate, checked against the live route table.

Two halves, as with the suspension and PIN guards:

  * structural — walk every registered route's dependency tree. Any route that
    resolves a platform owner must go through the gated
    get_platform_owner_context, EXCEPT /platform/setup/*, which must reach the
    owner through get_authenticated_platform_owner and never the gated one
    (or an owner with setup pending could not finish it);
  * behavioural — actually call every gated route with a pending owner's
    token and require the 403 + X-Security-Setup-Required the portal
    redirects on, then the same routes with a set-up owner and require that
    the gate is NOT what answers.

A new route that forgets the guard, or a setup route that picks up the wrong
dependency, fails here rather than in production.

The behavioural half needs a database (it creates owners), so it only runs
against a disposable one:

    PLATFORM_OWNER_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import os
import re
import uuid

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("PLATFORM_OWNER_FLOW_TEST_DATABASE_URL", "")
DB_OK = bool(FLOW_DB) and "throwaway" in FLOW_DB.rsplit("/", 1)[-1] and os.getenv("DATABASE_URL") == FLOW_DB
pytestmark = [
    pytest.mark.skipif(not DB_OK, reason="needs PLATFORM_OWNER_FLOW_TEST_DATABASE_URL (a throwaway database) == DATABASE_URL"),
    pytest.mark.asyncio(loop_scope="module"),
]

if DB_OK:
    import httpx
    from fastapi.routing import APIRoute

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import PlatformOwner
    from src.middleware.auth import (
        SECURITY_SETUP_REQUIRED_HEADER,
        get_authenticated_platform_owner,
        get_platform_owner_context,
    )
    from src.modules.auth.tokens import platform_owner_token_response
    from src.utils.auth import hash_password

SETUP_PREFIX = "/api/v1/platform/setup"
# Sanity floor: if the walk finds far fewer gated routes than exist, it has
# stopped seeing them (e.g. a refactor hid the dependency) and every
# assertion below would pass vacuously.
MIN_GATED_ROUTES = 40


def _calls(dependant, seen=None):
    seen = set() if seen is None else seen
    for dep in dependant.dependencies:
        if dep.call is not None:
            seen.add(dep.call)
        _calls(dep, seen)
    return seen


def owner_routes():
    """(route, method, gated?) for every route that resolves a platform owner."""
    out = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        calls = _calls(route.dependant)
        if get_authenticated_platform_owner not in calls:
            continue
        for method in sorted(route.methods):
            out.append((route, method, get_platform_owner_context in calls))
    return out


def concrete(path: str) -> str:
    """Fill path parameters with syntactically valid placeholders."""
    return re.sub(r"\{[^}]+\}", str(uuid.UUID(int=1)), path)


# ── Structural ────────────────────────────────────────────────────────────


def test_the_guard_marker_is_on_the_gated_dependency():
    assert getattr(get_platform_owner_context, "is_security_setup_guard", False) is True


def test_every_owner_route_outside_setup_is_gated():
    ungated = [f"{m} {r.path}" for r, m, gated in owner_routes() if not gated and not r.path.startswith(SETUP_PREFIX)]
    assert ungated == [], f"platform-owner routes missing the setup gate: {ungated}"
    gated = [r for r, m, g in owner_routes() if g]
    assert len(gated) >= MIN_GATED_ROUTES, f"walk found only {len(gated)} gated routes"


def test_setup_routes_are_never_gated():
    setup = [(r, m, g) for r, m, g in owner_routes() if r.path.startswith(SETUP_PREFIX)]
    assert len(setup) >= 7, [f"{m} {r.path}" for r, m, _ in setup]
    gated = [f"{m} {r.path}" for r, m, g in setup if g]
    assert gated == [], f"setup routes must not carry the gate: {gated}"


def test_auth_endpoints_do_not_resolve_an_owner_session():
    """Login, challenge and refresh must work with no session at all."""
    for r, _, _ in owner_routes():
        assert not r.path.startswith("/api/v1/platform/auth/"), r.path


# ── Behavioural ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture(loop_scope="module")
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def make_owner(*, setup_complete: bool) -> "PlatformOwner":
    async with async_session_factory() as db:
        owner = PlatformOwner(
            email=f"gate-{uuid.uuid4().hex[:10]}@throwaway.test", password_hash=hash_password("Owner-Passw0rd"),
            name="Gate", is_active=True, must_complete_security_setup=not setup_complete,
        )
        db.add(owner)
        await db.commit()
        return owner


def bearer(owner):
    return {"Authorization": f"Bearer {platform_owner_token_response(owner).access_token}"}


async def test_every_gated_route_answers_a_pending_owner_with_the_setup_redirect(client):
    headers = bearer(await make_owner(setup_complete=False))
    wrong = []
    checked = 0
    for route, method, gated in owner_routes():
        if not gated:
            continue
        res = await client.request(method, concrete(route.path), headers=headers, json={})
        checked += 1
        if res.status_code != 403 or res.headers.get(SECURITY_SETUP_REQUIRED_HEADER) != "1":
            wrong.append(f"{method} {route.path} -> {res.status_code} {res.headers.get(SECURITY_SETUP_REQUIRED_HEADER)}")
    assert checked >= MIN_GATED_ROUTES
    assert wrong == [], wrong


async def test_the_gate_does_not_answer_for_a_set_up_owner(client):
    """Set-up owner: the gate must not be what answers. GET routes only — past
    the gate the handler really runs, and the POST/PATCH/DELETE routes would
    mutate state. (The pending-owner test above can call every method safely:
    the gate stops each request before its handler.)"""
    headers = bearer(await make_owner(setup_complete=True))
    wrong = []
    for route, method, gated in owner_routes():
        if not gated or method != "GET":
            continue
        res = await client.request(method, concrete(route.path), headers=headers, json={})
        if res.headers.get(SECURITY_SETUP_REQUIRED_HEADER):
            wrong.append(f"{method} {route.path} -> {res.status_code}")
    assert wrong == [], wrong


async def test_setup_routes_stay_reachable_while_pending(client):
    headers = bearer(await make_owner(setup_complete=False))
    res = await client.get(f"{SETUP_PREFIX}/status", headers=headers)
    assert res.status_code == 200 and res.json()["must_complete_security_setup"] is True
    for route, method, _ in owner_routes():
        if route.path.startswith(SETUP_PREFIX):
            res = await client.request(method, concrete(route.path), headers=headers, json={})
            assert not res.headers.get(SECURITY_SETUP_REQUIRED_HEADER), f"{method} {route.path}"


async def test_login_is_not_gated(client):
    """A pending owner must still be able to sign in: that is how they reach setup."""
    owner = await make_owner(setup_complete=False)
    res = await client.post("/api/v1/platform/auth/login", json={"email": owner.email, "password": "Owner-Passw0rd"})
    assert res.status_code == 200 and "access_token" in res.json()
