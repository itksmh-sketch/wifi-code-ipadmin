"""Operator-admin permissions (P3): the matrix, and the route table against it.

Walks the live FastAPI route table, like test_audit_guard.py. Every route that
resolves an operator admin must declare exactly one permission
(permissions.require_perm), and the full route -> permission table must match
ROUTE_PERMISSIONS below. So:

  * a new admin route without a permission fails;
  * a new admin route with one fails until it is added here, which makes
    "who can call this" an explicit, reviewed line in the diff;
  * moving a route to another permission fails until it is moved here too.

Pure unit test: no server, no DB, no Redis.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from src.app import app
from src.middleware import auth, permissions
from src.middleware.auth import get_authenticated_admin, require_recent_pin
from src.middleware.permissions import ROLE_PERMISSIONS, permissions_for, require_perm

MATRIX = {
    "staff": {"self", "read", "vouchers.operate"},
    "admin": {"self", "read", "vouchers.operate", "vouchers.generate", "config", "money", "billing.read"},
    "superadmin": {"self", "read", "vouchers.operate", "vouchers.generate", "config", "money", "billing.read",
                   "secrets", "users", "audit"},
}

ROUTE_PERMISSIONS = {
    "self": {
        ("GET", "/api/v1/auth/me"),
        ("POST", "/api/v1/auth/me/password"),
        ("POST", "/api/v1/auth/me/phone"),
        ("POST", "/api/v1/auth/me/phone/verify"),
        ("POST", "/api/v1/auth/me/pin"),
        ("POST", "/api/v1/auth/me/pin/forgot"),
        ("POST", "/api/v1/auth/me/pin/reset"),
        ("POST", "/api/v1/auth/me/pin/verify"),
        ("GET", "/api/v1/auth/me/security"),
        ("POST", "/api/v1/auth/me/security-question"),
        ("POST", "/api/v1/auth/onboarding/otp/verify"),
        ("POST", "/api/v1/auth/onboarding/password"),
        ("POST", "/api/v1/auth/onboarding/phone"),
        ("GET", "/api/v1/auth/onboarding/status"),
    },
    "read": {
        ("GET", "/api/v1/admin/branding"),
        ("GET", "/api/v1/admin/routers"),
        ("GET", "/api/v1/admin/routers/{router_id}"),
        ("GET", "/api/v1/admin/routers/{router_id}/active-sessions"),
        ("GET", "/api/v1/admin/routers/{router_id}/health"),
        ("POST", "/api/v1/admin/routers/{router_id}/health/check"),
        ("GET", "/api/v1/admin/routers/{router_id}/interfaces"),
        ("GET", "/api/v1/admin/routers/{router_id}/metrics"),
        ("GET", "/api/v1/admin/routers/{router_id}/provision-logs"),
        ("GET", "/api/v1/admin/routers/{router_id}/provision-status/{log_id}"),
        ("GET", "/api/v1/admin/routers/{router_id}/setup/hotspot/detect"),
        ("GET", "/api/v1/admin/routers/{router_id}/setup/nat/detect"),
        ("GET", "/api/v1/admin/routers/{router_id}/setup/network/detect"),
        ("GET", "/api/v1/admin/routers/{router_id}/setup/radius/detect"),
        ("GET", "/api/v1/admin/routers/{router_id}/setup/status"),
        ("GET", "/api/v1/admin/routers/{router_id}/wireguard/status"),
        ("GET", "/api/v1/admin/sites"),
        ("GET", "/api/v1/analytics/snapshot"),
        ("GET", "/api/v1/analytics/trends"),
        ("GET", "/api/v1/billing/status"),
        ("GET", "/api/v1/dashboard"),
        ("GET", "/api/v1/payments"),
        ("GET", "/api/v1/payments/summary"),
        ("GET", "/api/v1/payments/{payment_id}"),
        ("GET", "/api/v1/plans"),
        ("GET", "/api/v1/providers"),
        ("GET", "/api/v1/sessions"),
        ("GET", "/api/v1/sessions/{session_id}"),
        ("GET", "/api/v1/sites/{site_id}/routers"),
        ("GET", "/api/v1/towns"),
        ("GET", "/api/v1/towns/{town_id}"),
        ("GET", "/api/v1/towns/{town_id}/sites"),
        ("GET", "/api/v1/vouchers"),
        ("GET", "/api/v1/vouchers/batch/{batch_id}"),
        ("GET", "/api/v1/vouchers/{voucher_id}"),
    },
    "vouchers.operate": {
        ("POST", "/api/v1/admin/routers/{router_id}/disconnect-user"),
        ("GET", "/api/v1/vouchers/print-batch"),
        ("POST", "/api/v1/vouchers/print-batch/confirm"),
        ("PUT", "/api/v1/vouchers/{voucher_id}/disable"),
        ("PUT", "/api/v1/vouchers/{voucher_id}/reactivate"),
    },
    "vouchers.generate": {
        ("POST", "/api/v1/vouchers/generate"),
    },
    "config": {
        ("PUT", "/api/v1/admin/branding"),
        ("POST", "/api/v1/admin/branding/logo"),
        ("POST", "/api/v1/admin/branding/preview-draft"),
        ("GET", "/api/v1/admin/branding/preview-token"),
        ("GET", "/api/v1/admin/commission-rules"),
        ("POST", "/api/v1/admin/commission-rules"),
        ("DELETE", "/api/v1/admin/commission-rules/{rule_id}"),
        ("PUT", "/api/v1/admin/commission-rules/{rule_id}"),
        ("GET", "/api/v1/admin/config-templates"),
        ("POST", "/api/v1/admin/config-templates"),
        ("DELETE", "/api/v1/admin/config-templates/{template_id}"),
        ("PUT", "/api/v1/admin/config-templates/{template_id}"),
        ("GET", "/api/v1/admin/resellers"),
        ("POST", "/api/v1/admin/resellers"),
        ("GET", "/api/v1/admin/resellers/{reseller_id}"),
        ("PUT", "/api/v1/admin/resellers/{reseller_id}"),
        ("GET", "/api/v1/admin/resellers/{reseller_id}/vouchers"),
        ("POST", "/api/v1/admin/routers/onboard"),
        ("POST", "/api/v1/admin/routers/temp-interfaces"),
        ("POST", "/api/v1/admin/routers/test-connection"),
        ("DELETE", "/api/v1/admin/routers/{router_id}"),
        ("POST", "/api/v1/admin/routers/{router_id}/apply-template"),
        ("POST", "/api/v1/admin/routers/{router_id}/credentials"),
        ("PUT", "/api/v1/admin/routers/{router_id}/credentials"),
        ("GET", "/api/v1/admin/routers/{router_id}/diagnostics"),
        ("POST", "/api/v1/admin/routers/{router_id}/provision"),
        ("POST", "/api/v1/admin/routers/{router_id}/reboot"),
        ("POST", "/api/v1/admin/routers/{router_id}/setup/hotspot/apply"),
        ("POST", "/api/v1/admin/routers/{router_id}/setup/nat/apply"),
        ("POST", "/api/v1/admin/routers/{router_id}/setup/network/apply"),
        ("POST", "/api/v1/admin/routers/{router_id}/setup/radius/apply"),
        ("DELETE", "/api/v1/admin/routers/{router_id}/wireguard"),
        ("POST", "/api/v1/admin/routers/{router_id}/wireguard/setup"),
        ("POST", "/api/v1/plans"),
        ("DELETE", "/api/v1/plans/{plan_id}"),
        ("PATCH", "/api/v1/plans/{plan_id}"),
        ("PUT", "/api/v1/plans/{plan_id}"),
        ("POST", "/api/v1/plans/{plan_id}/activate"),
        ("POST", "/api/v1/plans/{plan_id}/deactivate"),
        ("PUT", "/api/v1/sites/routers/{router_id}"),
        ("POST", "/api/v1/sites/{site_id}/routers"),
        ("GET", "/api/v1/sms-template/voucher"),
        ("PUT", "/api/v1/sms-template/voucher"),
        ("POST", "/api/v1/sms-template/voucher/preview"),
        ("POST", "/api/v1/towns"),
        ("PUT", "/api/v1/towns/{town_id}"),
        ("POST", "/api/v1/towns/{town_id}/sites"),
    },
    "money": {
        ("PUT", "/api/v1/admin/resellers/{reseller_id}/topup"),
        ("GET", "/api/v1/payments/export"),
        ("POST", "/api/v1/payments/reconcile"),
        ("PUT", "/api/v1/payments/{payment_id}/refund"),
        ("PUT", "/api/v1/payments/{payment_id}/verify"),
    },
    "billing.read": {
        ("GET", "/api/v1/billing/invoices"),
        ("GET", "/api/v1/billing/invoices/{invoice_id}"),
        ("GET", "/api/v1/billing/onboarding-checklist"),
        ("GET", "/api/v1/billing/sms-usage"),
        ("GET", "/api/v1/billing/transactions"),
    },
    "secrets": {
        ("GET", "/api/v1/admin/routers/{router_id}/nas-secret"),
        ("POST", "/api/v1/billing/invoices/{invoice_id}/pay"),
        ("GET", "/api/v1/payment-credentials"),
        ("DELETE", "/api/v1/payment-credentials/{provider}"),
        ("PUT", "/api/v1/payment-credentials/{provider}"),
        ("POST", "/api/v1/payment-credentials/{provider}/activate"),
        ("POST", "/api/v1/payment-credentials/{provider}/test"),
        ("GET", "/api/v1/sms-credentials"),
        ("POST", "/api/v1/sms-credentials/activate-platform"),
        ("DELETE", "/api/v1/sms-credentials/{provider}"),
        ("PUT", "/api/v1/sms-credentials/{provider}"),
        ("POST", "/api/v1/sms-credentials/{provider}/activate"),
        ("POST", "/api/v1/sms-credentials/{provider}/test"),
    },
    "users": {
        ("GET", "/api/v1/admin/users"),
        ("POST", "/api/v1/admin/users"),
        ("PATCH", "/api/v1/admin/users/{user_id}"),
        ("POST", "/api/v1/admin/users/{user_id}/disable"),
        ("POST", "/api/v1/admin/users/{user_id}/enable"),
    },
    "audit": {
        ("GET", "/api/v1/admin/audit"),
    },
}

# Routes that also need a recent PIN. P3 added the first three; every user
# management write needs one too.
PIN_GATED_IN_P3 = {
    ("GET", "/api/v1/payments/export"),
    ("PUT", "/api/v1/payments/{payment_id}/refund"),
    ("GET", "/api/v1/admin/routers/{router_id}/nas-secret"),
    ("POST", "/api/v1/admin/users"),
    ("PATCH", "/api/v1/admin/users/{user_id}"),
    ("POST", "/api/v1/admin/users/{user_id}/disable"),
    ("POST", "/api/v1/admin/users/{user_id}/enable"),
}

# Onboarding must stay reachable on a temp password: resolved through
# get_authenticated_admin, not get_current_user.
ONBOARDING = {
    ("POST", "/api/v1/auth/onboarding/otp/verify"),
    ("POST", "/api/v1/auth/onboarding/password"),
    ("POST", "/api/v1/auth/onboarding/phone"),
    ("GET", "/api/v1/auth/onboarding/status"),
}


def _calls(dependant, acc=None):
    acc = [] if acc is None else acc
    for d in dependant.dependencies:
        acc.append(d.call)
        _calls(d, acc)
    return acc


def _admin_routes():
    """(method, path) -> list of dependency callables, for every route that
    resolves an operator admin."""
    out = {}
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        calls = _calls(r.dependant)
        if get_authenticated_admin not in calls:
            continue
        for m in r.methods - {"HEAD", "OPTIONS"}:
            out[(m, r.path)] = calls
    return out


def _perm_checks(calls):
    seen, out = set(), []
    for c in calls:
        if hasattr(c, "permission") and id(c) not in seen:
            seen.add(id(c))
            out.append(c)
    return out


def test_matrix_snapshot():
    assert {role: set(perms) for role, perms in ROLE_PERMISSIONS.items()} == MATRIX


def test_roles_are_nested():
    assert MATRIX["staff"] < MATRIX["admin"] < MATRIX["superadmin"]
    assert permissions.ALL_PERMISSIONS == MATRIX["superadmin"]


def test_every_admin_route_declares_exactly_one_permission():
    problems = {k: [c.permission for c in _perm_checks(v)] for k, v in _admin_routes().items()
                if len(_perm_checks(v)) != 1}
    assert problems == {}


def test_route_table_matches_the_snapshot():
    expected = {route: perm for perm, routes in ROUTE_PERMISSIONS.items() for route in routes}
    actual = {k: _perm_checks(v)[0].permission for k, v in _admin_routes().items() if _perm_checks(v)}
    missing = sorted(set(expected) - set(actual))
    unlisted = sorted(set(actual) - set(expected))
    moved = sorted((k, expected[k], actual[k]) for k in set(expected) & set(actual) if expected[k] != actual[k])
    assert (missing, unlisted, moved) == ([], [], [])


def test_snapshot_lists_each_route_once():
    seen = [route for routes in ROUTE_PERMISSIONS.values() for route in routes]
    assert len(seen) == len(set(seen))


def test_pin_gates():
    routes = _admin_routes()
    assert {k for k in PIN_GATED_IN_P3 if require_recent_pin not in routes[k]} == set()


def test_onboarding_routes_skip_the_onboarding_gate_and_nothing_else_does():
    routes = _admin_routes()
    skipping = {k for k, calls in routes.items() if auth.get_current_user not in calls}
    assert skipping == ONBOARDING


def test_superadmin_keeps_every_route():
    """No existing superadmin loses access: every admin route's permission is
    one the superadmin role holds."""
    used = {c.permission for calls in _admin_routes().values() for c in _perm_checks(calls)}
    assert used <= MATRIX["superadmin"]


def test_staff_cannot_generate_vouchers_or_touch_money_config_or_secrets():
    for perm in ("vouchers.generate", "config", "money", "billing.read", "secrets", "users", "audit"):
        assert perm not in permissions_for("staff")


def test_unknown_role_gets_nothing():
    assert permissions_for("viewer") == frozenset()
    assert permissions_for(None) == frozenset()


def test_unknown_permission_is_a_programming_error():
    with pytest.raises(ValueError):
        require_perm("vouchers.write")


def test_old_role_helpers_are_gone():
    assert not hasattr(auth, "require_role")
    assert not hasattr(auth, "require_active_role")
