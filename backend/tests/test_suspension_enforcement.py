"""Suspension enforcement: what a suspended operator can and cannot do.

Two layers:

1. **Route-table policy** — walks the live FastAPI dependency graph and asserts
   the suspension guard is present on exactly the endpoints policy says it
   should be, and ABSENT from the exemption chain. This is the test that
   matters most: it catches a future endpoint added without the guard, and it
   catches someone sweeping the guard onto the one path out of suspension.

2. **Guard behaviour** — that the guard actually raises 403 for a suspended
   operator and passes everyone else through.

THE EXEMPTION (see test_exemption_chain_is_never_suspension_gated): paying the
overdue invoice is the only way out of suspension. Blocking POST
/billing/invoices/{id}/pay — or any of the four PIN endpoints it depends on
through require_recent_pin — creates a permanent, unrecoverable lockout.

Pure unit tests: no server, no DB, no Redis. Defines neither BASE_URL nor
_request, so tests/conftest.py classifies this module as unit-only.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from src.app import app
from src.db.models import AdminUser, ISPOperator
from src.middleware.auth import (
    SUSPENDED_DETAIL,
    assert_operator_not_suspended,
    require_active_role,
)

# --------------------------------------------------------------------------
# The policy, as data. (method, path) exactly as FastAPI registers it.
# --------------------------------------------------------------------------

API = "/api/v1"

MUST_BE_GATED = [
    # Router fleet — everything that brings capacity into service.
    ("POST",   f"{API}/admin/routers/onboard"),
    ("POST",   f"{API}/admin/routers/{{router_id}}/provision"),
    ("POST",   f"{API}/admin/routers/{{router_id}}/apply-template"),
    ("POST",   f"{API}/admin/routers/{{router_id}}/setup/network/apply"),
    ("POST",   f"{API}/admin/routers/{{router_id}}/setup/hotspot/apply"),
    ("POST",   f"{API}/admin/routers/{{router_id}}/setup/radius/apply"),
    ("POST",   f"{API}/admin/routers/{{router_id}}/setup/nat/apply"),
    ("POST",   f"{API}/sites/{{site_id}}/routers"),
    ("PUT",    f"{API}/sites/routers/{{router_id}}"),
    ("POST",   f"{API}/admin/routers/{{router_id}}/wireguard/setup"),
    # Sellable inventory.
    ("POST",   f"{API}/plans"),
    ("PUT",    f"{API}/plans/{{plan_id}}"),
    ("PATCH",  f"{API}/plans/{{plan_id}}"),
    ("POST",   f"{API}/plans/{{plan_id}}/activate"),
    ("POST",   f"{API}/vouchers/generate"),
    ("PUT",    f"{API}/vouchers/{{voucher_id}}/reactivate"),
    # Footprint.
    ("POST",   f"{API}/towns"),
    ("POST",   f"{API}/towns/{{town_id}}/sites"),
    # Distribution channel.
    ("POST",   f"{API}/admin/resellers"),
    ("PUT",    f"{API}/admin/resellers/{{reseller_id}}/topup"),
    # Re-arming the till / the gateway (the shared credentials factory, once
    # per category).
    ("PUT",    f"{API}/payment-credentials/{{provider}}"),
    ("POST",   f"{API}/payment-credentials/{{provider}}/activate"),
    ("PUT",    f"{API}/sms-credentials/{{provider}}"),
    ("POST",   f"{API}/sms-credentials/{{provider}}/activate"),
    ("POST",   f"{API}/sms-credentials/activate-platform"),
    # Branding + SMS template (pre-existing gates, asserted so they stay).
    ("PUT",    f"{API}/admin/branding"),
    ("POST",   f"{API}/admin/branding/logo"),
    ("PUT",    f"{API}/sms-template/voucher"),
]

# The way out of suspension, and everything a paying-but-suspended operator
# needs to reach it. A guard appearing on ANY of these is a permanent lockout.
EXEMPTION_CHAIN = [
    ("POST",   f"{API}/billing/invoices/{{invoice_id}}/pay"),
    ("GET",    f"{API}/billing/payment-callback"),
    ("GET",    f"{API}/billing/status"),
    ("GET",    f"{API}/billing/invoices"),
    # require_recent_pin gates pay_invoice, so the whole PIN chain is
    # load-bearing for the exemption.
    ("POST",   f"{API}/auth/me/pin"),
    ("POST",   f"{API}/auth/me/pin/verify"),
    ("POST",   f"{API}/auth/me/pin/forgot"),
    ("POST",   f"{API}/auth/me/pin/reset"),
    # Sign-in stays open: see, then pay.
    ("POST",   f"{API}/auth/login"),
    ("POST",   f"{API}/auth/refresh"),
    ("POST",   f"{API}/reseller/auth/login"),
    # An already-paid customer must always be able to complete. (The webhook
    # router carries its own absolute prefix — it is not mounted under API.)
    ("POST",   "/api/v1/webhooks/paystack/{operator_slug}"),
    ("POST",   "/api/v1/webhooks/flutterwave/{operator_slug}"),
    ("POST",   "/api/v1/webhooks/platform-billing/paystack"),
    ("PUT",    f"{API}/payments/{{payment_id}}/verify"),
    ("PUT",    f"{API}/payments/{{payment_id}}/refund"),
    ("POST",   f"{API}/payments/reconcile"),
    # Winding down / reducing access stays open.
    ("DELETE", f"{API}/admin/routers/{{router_id}}"),
    ("POST",   f"{API}/admin/routers/{{router_id}}/disconnect-user"),
    ("POST",   f"{API}/admin/routers/{{router_id}}/reboot"),
    ("PUT",    f"{API}/vouchers/{{voucher_id}}/disable"),
    ("DELETE", f"{API}/plans/{{plan_id}}"),
    ("POST",   f"{API}/plans/{{plan_id}}/deactivate"),
    ("DELETE", f"{API}/admin/routers/{{router_id}}/wireguard"),
    # Account self-service — blocking any of it risks lockout.
    ("POST",   f"{API}/auth/me/password"),
    ("POST",   f"{API}/auth/reset/request"),
    ("POST",   f"{API}/auth/reset/set-password"),
    # Diagnostics and previews change nothing.
    ("GET",    f"{API}/admin/routers/{{router_id}}/diagnostics"),
    ("POST",   f"{API}/admin/routers/test-connection"),
    ("POST",   f"{API}/payment-credentials/{{provider}}/test"),
    ("POST",   f"{API}/sms-credentials/{{provider}}/test"),
    ("POST",   f"{API}/admin/branding/preview-draft"),
]


def _guards_in(dependant) -> bool:
    """True if the suspension guard appears anywhere in this dependant tree."""
    if getattr(dependant.call, "is_suspension_guard", False):
        return True
    return any(_guards_in(sub) for sub in dependant.dependencies)


def _route_index():
    index = {}
    for route in app.routes:
        for method in getattr(route, "methods", None) or ():
            index[(method, route.path)] = route
    return index


ROUTES = _route_index()


def _lookup(method: str, path: str):
    route = ROUTES.get((method, path))
    assert route is not None, (
        f"{method} {path} is not registered. If the route was renamed or moved, "
        f"update this test's policy list — do not delete the entry."
    )
    return route


@pytest.mark.parametrize("method,path", MUST_BE_GATED, ids=lambda v: v if isinstance(v, str) else v)
def test_blocked_routes_carry_the_suspension_guard(method, path):
    route = _lookup(method, path)
    assert _guards_in(route.dependant), (
        f"{method} {path} creates sellable capacity but is NOT suspension-gated — "
        f"a suspended operator can still reach it."
    )


@pytest.mark.parametrize("method,path", EXEMPTION_CHAIN, ids=lambda v: v if isinstance(v, str) else v)
def test_exemption_chain_is_never_suspension_gated(method, path):
    route = _lookup(method, path)
    assert not _guards_in(route.dependant), (
        f"{method} {path} MUST stay reachable while suspended. Gating it either "
        f"blocks the only path out of suspension (permanent lockout), denies a "
        f"customer who already paid, or blocks an action that reduces access."
    )


def test_policy_lists_do_not_overlap():
    assert not (set(MUST_BE_GATED) & set(EXEMPTION_CHAIN))


# --------------------------------------------------------------------------
# Guard behaviour
# --------------------------------------------------------------------------


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeDb:
    def __init__(self, value):
        self._value = value
        self.executed = []

    async def execute(self, statement):
        self.executed.append(statement)
        return _ScalarResult(self._value)


def _operator(status: str) -> ISPOperator:
    return ISPOperator(
        id=uuid.uuid4(), name="Osu Hotspot", slug="osu-hotspot",
        contact_email="ops@example.test", status=status,
    )


def _admin(operator_id, role="admin") -> AdminUser:
    return AdminUser(
        id=uuid.uuid4(), isp_operator_id=operator_id,
        email="admin@example.test", role=role, is_active=True,
    )


@pytest.mark.asyncio
async def test_guard_raises_403_for_suspended_operator():
    operator = _operator("suspended")
    with pytest.raises(HTTPException) as exc:
        await assert_operator_not_suspended(FakeDb(operator), operator.id)
    assert exc.value.status_code == 403
    assert exc.value.detail == SUSPENDED_DETAIL


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["approved", "pending", "cancelled"])
async def test_guard_passes_non_suspended_operators(status):
    operator = _operator(status)
    await assert_operator_not_suspended(FakeDb(operator), operator.id)


@pytest.mark.asyncio
async def test_guard_treats_missing_operator_row_as_not_suspended():
    """Failing closed here would lock an operator out over a data problem."""
    await assert_operator_not_suspended(FakeDb(None), uuid.uuid4())


@pytest.mark.asyncio
async def test_require_active_role_blocks_suspended_operator():
    operator = _operator("suspended")
    check = require_active_role("superadmin", "admin")
    with pytest.raises(HTTPException) as exc:
        await check(user=_admin(operator.id), db=FakeDb(operator))
    assert exc.value.status_code == 403
    assert exc.value.detail == SUSPENDED_DETAIL


@pytest.mark.asyncio
async def test_require_active_role_still_enforces_role_first():
    """Role failure must not leak whether the operator is suspended."""
    operator = _operator("approved")
    check = require_active_role("superadmin")
    with pytest.raises(HTTPException) as exc:
        await check(user=_admin(operator.id, role="viewer"), db=FakeDb(operator))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Insufficient permissions"


@pytest.mark.asyncio
async def test_require_active_role_returns_admin_user_unchanged():
    """Several routes annotate this as TenantContext and use .isp_operator_id —
    the return type must stay AdminUser."""
    operator = _operator("approved")
    user = _admin(operator.id)
    check = require_active_role("superadmin", "admin")
    assert await check(user=user, db=FakeDb(operator)) is user


# --------------------------------------------------------------------------
# End-to-end through the real ASGI stack: the guard actually bites, and the
# exemption actually gets through. Auth and the DB session are overridden;
# routing, dependency resolution and error handling are the real thing.
# --------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from src.db.base import get_db  # noqa: E402
from src.middleware.auth import get_current_user  # noqa: E402


class QueueDb:
    """Returns queued rows in order; None once exhausted."""

    def __init__(self, rows):
        self.rows = list(rows)

    async def execute(self, statement):
        return _ScalarResult(self.rows.pop(0) if self.rows else None)

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _client(operator: ISPOperator, *, rows=None, pin_elevated=False) -> TestClient:
    """`rows` is the exact DB result queue for the request.

    Note the queue differs by route on purpose: a suspension-gated route spends
    its first query on the guard's own ISPOperator lookup, while an exempt route
    has no guard and so spends it on whatever the handler asks for first. That
    asymmetry is itself the thing under test.
    """
    user = _admin(operator.id, role="superadmin")
    user.must_complete_onboarding = False
    user.must_change_password = False
    if pin_elevated:
        user.pin_hash = "x"
        user.pin_locked_until = None
        user.pin_verified_until = datetime.now(timezone.utc) + timedelta(minutes=10)

    db = QueueDb(rows if rows is not None else [operator])

    async def _user():
        return user

    async def _db():
        yield db

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = _db
    client = TestClient(app, raise_server_exceptions=False)
    client._cleanup = lambda: app.dependency_overrides.clear()
    return client


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_blocked_route_returns_403_while_suspended():
    operator = _operator("suspended")
    with _client(operator) as client:
        res = client.post(f"/api/v1/plans/{uuid.uuid4()}/activate")
    assert res.status_code == 403
    assert res.json()["detail"] == SUSPENDED_DETAIL


def test_same_route_is_not_suspension_blocked_when_approved():
    """Approved operator gets past the guard — the 404 is the handler talking."""
    operator = _operator("approved")
    with _client(operator, rows=[operator, None]) as client:
        res = client.post(f"/api/v1/plans/{uuid.uuid4()}/activate")
    assert res.status_code != 403
    assert SUSPENDED_DETAIL not in res.text


def test_pay_invoice_is_reachable_while_suspended():
    """THE EXEMPTION. A suspended operator must be able to pay their invoice.

    The 404 means the guard let the request through and the handler ran and
    found no such invoice — which is exactly what a bogus invoice id should do.
    A 403 here would mean permanent lockout.
    """
    operator = _operator("suspended")
    # No operator row queued first: pay_invoice has no guard, so its very first
    # query is the invoice lookup. Returning None there gives a clean 404.
    with _client(operator, rows=[None], pin_elevated=True) as client:
        res = client.post(f"/api/v1/billing/invoices/{uuid.uuid4()}/pay")
    assert res.status_code == 404, res.text
    assert SUSPENDED_DETAIL not in res.text


def test_pin_verify_is_reachable_while_suspended():
    """pay_invoice is PIN-gated, so the PIN chain must work while suspended."""
    operator = _operator("suspended")
    with _client(operator, rows=[]) as client:
        res = client.post("/api/v1/auth/me/pin/verify", json={"pin": "0000"})
    assert res.status_code != 403 or SUSPENDED_DETAIL not in res.text
