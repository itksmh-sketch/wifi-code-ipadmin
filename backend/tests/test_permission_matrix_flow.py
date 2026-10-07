"""The permission matrix through the real stack (P3), against a REAL database.

For each role (staff, admin, superadmin) this signs in a real user and calls
EVERY operator-admin route in the live route table:

  * a route whose permission the role lacks must answer 403 with the
    permission message — before any PIN, suspension or body check;
  * a route whose permission the role holds must get past the permission
    check (any other status: 2xx, or 404/422 for the placeholder ids and
    empty bodies used here).

So staff is checked against every route of every module, and superadmin is
shown to keep every route. Ids are random UUIDs and bodies are empty, so
allowed calls mostly stop at 404/422; any that do run, run on a throwaway.

    MATRIX_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("MATRIX_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs MATRIX_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from fastapi.routing import APIRoute

    from src.app import app
    from sqlalchemy import update

    from src.db.base import async_session_factory
    from src.db.models import AdminUser, ISPOperator
    from src.middleware.auth import get_authenticated_admin
    from src.middleware.permissions import PERMISSION_DENIED, ROLE_PERMISSIONS
    from src.utils.auth import hash_password

PASSWORD = "Str0ngPassword"
PATH_VALUES = {"provider": "paystack", "batch_id": "no-such-batch"}


def _calls(dependant, acc):
    for d in dependant.dependencies:
        acc.append(d.call)
        _calls(d, acc)
    return acc


def admin_routes() -> list[tuple[str, str, str]]:
    """(method, path, permission) for every operator-admin route."""
    out = []
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        calls = _calls(r.dependant, [])
        if get_authenticated_admin not in calls:
            continue
        [perm] = {c.permission for c in calls if hasattr(c, "permission")}
        for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
            out.append((m, r.path, perm))
    return sorted(out, key=lambda x: (x[1], x[0]))


def concrete(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: PATH_VALUES.get(m.group(1), str(uuid.uuid4())), path)


@pytest_asyncio.fixture(loop_scope="module")
async def client(monkeypatch):
    async def no_limit(*args, **kwargs):
        return None

    for name, module in list(sys.modules.items()):
        if name.startswith("src.") and hasattr(module, "enforce_rate_limit"):
            monkeypatch.setattr(module, "enforce_rate_limit", no_limit)
    # An unhandled exception becomes a 500 (noted in the report) instead of
    # aborting the sweep: a permitted call with placeholder input may crash.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def signed_in(client, role) -> dict:
    """A fresh operator with one onboarded, PIN-elevated user of ``role``."""
    slug = f"mx-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Mx {slug}", slug=slug, contact_email=f"{slug}@throwaway.test", status="approved")
        db.add(operator)
        await db.flush()
        admin = AdminUser(
            isp_operator_id=operator.id, email=f"{slug}@throwaway.test", password_hash=hash_password(PASSWORD),
            role=role, is_active=True, phone="233244000777", phone_verified=True, must_complete_onboarding=False,
            must_change_password=False, pin_hash=hash_password("482913"), pin_set_at=now,
            pin_verified_until=now + timedelta(hours=1),
        )
        db.add(admin)
        await db.commit()
    res = await client.post("/api/v1/auth/login", json={"email": admin.email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    # Sign-in ends any PIN elevation; elevate after it.
    async with async_session_factory() as db:
        await db.execute(update(AdminUser).where(AdminUser.id == admin.id).values(pin_verified_until=now + timedelta(hours=1)))
        await db.commit()
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


async def call(client, headers, method, path):
    kwargs = {"headers": headers}
    if method in {"POST", "PUT", "PATCH"}:
        kwargs["json"] = {}
    return await client.request(method, concrete(path), **kwargs)


def is_denied(res) -> bool:
    if res.status_code != 403:
        return False
    try:
        return res.json().get("detail") == PERMISSION_DENIED
    except ValueError:
        return False


def test_the_route_table_is_what_this_suite_expects():
    routes = admin_routes()
    assert len(routes) >= 130
    assert {perm for _, _, perm in routes} <= set(ROLE_PERMISSIONS["superadmin"])


@pytest.mark.parametrize("role", ["staff", "admin", "superadmin"])
async def test_every_route_against_the_matrix(client, role):
    headers = await signed_in(client, role)
    allowed = ROLE_PERMISSIONS[role]
    wrong, server_errors = [], []
    for method, path, perm in admin_routes():
        res = await call(client, headers, method, path)
        if is_denied(res) == (perm in allowed):
            wrong.append((method, path, perm, res.status_code, res.text[:120]))
        if res.status_code >= 500:
            server_errors.append((method, path, res.status_code))
    # Printed for the report; a 5xx on a placeholder id is noted, not failed.
    print(f"\n{role}: {len(admin_routes())} routes, server errors on placeholder input: {server_errors}")
    assert wrong == []


async def test_staff_sample_route_per_module_is_403(client):
    """The approved spot check: one write per module that staff must not reach."""
    headers = await signed_in(client, "staff")
    sample = [
        ("POST", "/api/v1/vouchers/generate"),
        ("POST", "/api/v1/plans"),
        ("POST", "/api/v1/towns"),
        ("POST", "/api/v1/sites/{site_id}/routers"),
        ("POST", "/api/v1/admin/routers/onboard"),
        ("POST", "/api/v1/admin/routers/{router_id}/setup/radius/apply"),
        ("POST", "/api/v1/admin/routers/{router_id}/wireguard/setup"),
        ("GET", "/api/v1/admin/routers/{router_id}/nas-secret"),
        ("POST", "/api/v1/admin/config-templates"),
        ("PUT", "/api/v1/admin/branding"),
        ("PUT", "/api/v1/sms-template/voucher"),
        ("POST", "/api/v1/admin/resellers"),
        ("POST", "/api/v1/admin/commission-rules"),
        ("PUT", "/api/v1/payments/{payment_id}/refund"),
        ("GET", "/api/v1/payments/export"),
        ("PUT", "/api/v1/payment-credentials/{provider}"),
        ("PUT", "/api/v1/sms-credentials/{provider}"),
        ("POST", "/api/v1/sms-credentials/activate-platform"),
        ("GET", "/api/v1/billing/invoices"),
        ("POST", "/api/v1/billing/invoices/{invoice_id}/pay"),
        ("GET", "/api/v1/admin/users"),
        ("POST", "/api/v1/admin/users"),
    ]
    results = [(m, p, is_denied(await call(client, headers, m, p))) for m, p in sample]
    assert [r for r in results if not r[2]] == []


async def test_staff_keeps_their_day_to_day_routes(client):
    headers = await signed_in(client, "staff")
    for path in ("/api/v1/dashboard", "/api/v1/vouchers", "/api/v1/plans", "/api/v1/sessions",
                 "/api/v1/admin/routers", "/api/v1/billing/status", "/api/v1/admin/branding", "/api/v1/auth/me"):
        res = await client.get(path, headers=headers)
        assert res.status_code == 200, (path, res.status_code, res.text[:200])
