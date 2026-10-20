"""Router diagnostics never hand an admin the router's secrets, against a REAL
database: GET /api/v1/admin/routers/{id}/diagnostics, as each role that may
call it, for a router whose RADIUS entry is right (the check passes) and one
whose entry points elsewhere (it fails and lists the entries). The router's
RADIUS secret (which /radius print returns to an API user with 'sensitive'),
its API password and its NAS secret must appear nowhere in the response.

Only the RouterOS connection is faked (MikroTikAPIService.connect): sign-in,
permissions, the tenant check, credential decryption and the diagnostics
service are real. Runs the FastAPI app in-process against a disposable
database:

    ROUTER_DIAGNOSTICS_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("ROUTER_DIAGNOSTICS_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs ROUTER_DIAGNOSTICS_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import update

    from src.app import app
    from src.config import get_settings
    from src.db.base import async_session_factory
    from src.db.models import AdminUser, ISPOperator, Router, RouterCredential, Site, Town
    from src.middleware.permissions import PERMISSION_DENIED
    from src.modules.mikrotik.api_service import MikroTikAPIService, MikroTikConnection
    from src.utils.auth import hash_password
    from src.utils.encryption import encrypt_secret

PASSWORD = "Str0ngPassword"
RADIUS_SECRET = "S3cret-RADIUS-XYZZY"
API_PASSWORD = "Api-Pass-QWERTY-7731"
NAS_SECRET = "Nas-Secret-PLUGH-4417"


class FakeResource:
    def __init__(self, rows):
        self.rows = rows

    def call(self, command, arguments=None, queries=None):
        return [dict(r) for r in self.rows] if command == "print" else []


class FakeApi:
    def __init__(self, radius_address):
        self.paths = {
            "/system/resource": [{"version": "7.22.3 (stable)", "board-name": "CHR", "uptime": "1d2h3m",
                                  "cpu-load": "3", "free-memory": "100", "total-memory": "200"}],
            "/radius": [{"id": "*1", "service": "hotspot", "address": radius_address, "secret": RADIUS_SECRET,
                         "authentication-port": "1812", "accounting-port": "1813", "disabled": "false"}],
            "/ip/hotspot": [{"id": "*1", "name": "hotspot1", "profile": "hsprof1", "disabled": "false"}],
            "/ip/hotspot/active": [],
            "/interface": [{"id": "*1", "name": "ether1", "type": "ether", "running": "true", "disabled": "false"}],
        }

    def get_resource(self, path):
        return FakeResource(self.paths.get(path, []))


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    async def no_limit(*args, **kwargs):
        return None

    for name, module in list(sys.modules.items()):
        if name.startswith("src.") and hasattr(module, "enforce_rate_limit"):
            monkeypatch.setattr(module, "enforce_rate_limit", no_limit)

    radius_address_by_host: dict[str, str] = {}
    connects: list[dict] = []

    def fake_connect(self, *, host, port, username, password, use_ssl):
        connects.append({"host": host, "username": username, "password": password})
        return MikroTikConnection(pool=None, api=FakeApi(radius_address_by_host[host]))

    monkeypatch.setattr(MikroTikAPIService, "connect", fake_connect)
    monkeypatch.setattr(MikroTikAPIService, "disconnect", lambda self, connection: None)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield {"client": client, "radius_address_by_host": radius_address_by_host, "connects": connects}


# ── fixtures ──────────────────────────────────────────────────────────────

async def make_operator() -> "ISPOperator":
    slug = f"dg-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Dg {slug}", slug=slug, contact_email=f"{slug}@throwaway.test", status="approved")
        db.add(operator)
        await db.commit()
        return operator


async def signed_in(client, operator, role) -> dict:
    """An onboarded, PIN-elevated user of ``role`` at ``operator``."""
    email = f"{role}-{uuid.uuid4().hex[:8]}@throwaway.test"
    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        admin = AdminUser(
            isp_operator_id=operator.id, email=email, password_hash=hash_password(PASSWORD),
            role=role, is_active=True, phone="233244000777", phone_verified=True, must_complete_onboarding=False,
            must_change_password=False, pin_hash=hash_password("482913"), pin_set_at=now,
        )
        db.add(admin)
        await db.commit()
    res = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    async with async_session_factory() as db:  # sign-in ends any PIN elevation; elevate after it
        await db.execute(update(AdminUser).where(AdminUser.id == admin.id).values(pin_verified_until=now + timedelta(hours=1)))
        await db.commit()
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


_TUNNEL = iter(range(10, 250))


async def make_router(env, operator, *, radius_address) -> "Router":
    tunnel_ip = f"10.102.{next(_TUNNEL)}.2"
    env["radius_address_by_host"][tunnel_ip] = radius_address
    async with async_session_factory() as db:
        town = Town(isp_operator_id=operator.id, name=f"T {uuid.uuid4().hex[:6]}", region="Volta")
        db.add(town)
        await db.flush()
        site = Site(isp_operator_id=operator.id, town_id=town.id, name="S", address="1 Test Road")
        db.add(site)
        await db.flush()
        router = Router(isp_operator_id=operator.id, site_id=site.id, name=f"R-{uuid.uuid4().hex[:6]}",
                        nas_identifier=f"nas-{uuid.uuid4().hex[:6]}", nas_secret=encrypt_secret(NAS_SECRET),
                        nas_secret_plain=NAS_SECRET, wg_enabled=True, wg_tunnel_ip=tunnel_ip,
                        is_active=True, is_online=True)
        db.add(router)
        await db.flush()
        db.add(RouterCredential(router_id=router.id, api_username="admin",
                                api_password_encrypted=encrypt_secret(API_PASSWORD)))
        await db.commit()
        return router


async def diagnostics(env, headers, router):
    return await env["client"].get(f"/api/v1/admin/routers/{router.id}/diagnostics", headers=headers)


def radius_check(body) -> dict:
    return next(c for c in body["checks"] if c["name"] == "radius_config")


def assert_no_secrets(text):
    for secret in (RADIUS_SECRET, API_PASSWORD, NAS_SECRET):
        assert secret not in text, f"{secret!r} in the diagnostics response"


# ── tests ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["admin", "superadmin"])
async def test_passing_radius_check_carries_no_secret(env, role):
    operator = await make_operator()
    headers = await signed_in(env["client"], operator, role)
    router = await make_router(env, operator, radius_address=get_settings().wg_server_tunnel_ip)
    res = await diagnostics(env, headers, router)
    assert res.status_code == 200, res.text
    assert_no_secrets(res.text)
    check = radius_check(res.json())
    assert check["status"] == "pass"
    (entry,) = check["data"]["radius_entries"]
    assert "secret" not in entry and entry["secret_configured"] is True
    assert entry["address"] == get_settings().wg_server_tunnel_ip
    # The real credentials were used for the (fake) router connection.
    assert {"host": str(router.wg_tunnel_ip), "username": "admin", "password": API_PASSWORD} in env["connects"]


@pytest.mark.parametrize("role", ["admin", "superadmin"])
async def test_failing_radius_check_lists_entries_without_the_secret(env, role):
    operator = await make_operator()
    headers = await signed_in(env["client"], operator, role)
    router = await make_router(env, operator, radius_address="34.122.11.114")
    res = await diagnostics(env, headers, router)
    assert res.status_code == 200, res.text
    assert_no_secrets(res.text)
    check = radius_check(res.json())
    assert check["status"] == "fail" and "does not point to" in check["message"]
    (entry,) = check["data"]["radius_entries"]
    assert entry["address"] == "34.122.11.114" and "secret" not in entry and entry["secret_configured"] is True


async def test_staff_cannot_run_diagnostics(env):
    operator = await make_operator()
    headers = await signed_in(env["client"], operator, "staff")
    router = await make_router(env, operator, radius_address=get_settings().wg_server_tunnel_ip)
    res = await diagnostics(env, headers, router)
    assert res.status_code == 403 and res.json().get("detail") == PERMISSION_DENIED
    assert_no_secrets(res.text)


async def test_another_operators_router_is_not_diagnosed(env):
    operator, other = await make_operator(), await make_operator()
    headers = await signed_in(env["client"], operator, "admin")
    router = await make_router(env, other, radius_address=get_settings().wg_server_tunnel_ip)
    res = await diagnostics(env, headers, router)
    assert res.status_code == 404, res.text
    assert_no_secrets(res.text)
