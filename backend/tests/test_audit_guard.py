"""Audit coverage: every state-changing operator or platform-owner route is
audited, deliberately exempt, or on the P4 backlog below. Nothing else.

Walks the live FastAPI route table, like test_suspension_enforcement.py. A
route counts as audited when its dependency tree carries the
``modules.audit.audited`` marker, which also captures the request's ip, user
agent and request id for the rows the handler writes.

In scope: every non-GET route that resolves an operator admin or a platform
owner, plus the unauthenticated sign-in and recovery routes under /auth and
/platform/auth. Public routes (captive portal, reseller, webhooks, apply) are
out of scope.

What fails:
  * a new in-scope route that is in none of the three lists;
  * a route that is marked audited but still listed as pending or exempt;
  * a list entry naming a route that no longer exists.

P4 shrinks PENDING batch by batch until it is empty.

Pure unit test: no server, no DB, no Redis.
"""
from __future__ import annotations

from fastapi.routing import APIRoute

from src.app import app
from src.middleware.auth import get_authenticated_admin, get_authenticated_platform_owner

_AUTH = {get_authenticated_admin, get_authenticated_platform_owner}
_ANON_PREFIXES = ("/api/v1/auth/", "/api/v1/platform/auth/")
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}

AUDITED = {
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/auth/me/phone/verify"),
    ("POST", "/api/v1/auth/me/pin"),
    ("POST", "/api/v1/auth/me/pin/reset"),
    ("POST", "/api/v1/auth/me/pin/verify"),
    ("POST", "/api/v1/auth/me/security-question"),
    # P2: disabling sign-in
    ("POST", "/api/v1/platform/operators/{operator_id}/admin-logins/disable"),
    ("POST", "/api/v1/platform/operators/{operator_id}/admin-logins/enable"),
    ("POST", "/api/v1/platform/operators/{operator_id}/admins/{admin_id}/disable"),
    ("POST", "/api/v1/platform/operators/{operator_id}/admins/{admin_id}/enable"),
}

# Change nothing worth a row: token renewal, previews, and a check that only
# reports whether a typed code matches.
EXEMPT = {
    ("POST", "/api/v1/auth/refresh"),
    ("POST", "/api/v1/platform/auth/refresh"),
    ("POST", "/api/v1/admin/branding/preview-draft"),
    ("POST", "/api/v1/sms-template/voucher/preview"),
    ("POST", "/api/v1/platform/notification-templates/{event}/{channel}/preview"),
    ("POST", "/api/v1/platform/setup/character-code/check"),
}

# P4 backlog, one comment per batch.
PENDING = {
    # Operator accounts (onboarding, password, reset, phone, PIN recovery)
    ("POST", "/api/v1/auth/me/password"),
    ("POST", "/api/v1/auth/me/phone"),
    ("POST", "/api/v1/auth/me/pin/forgot"),
    ("POST", "/api/v1/auth/onboarding/otp/verify"),
    ("POST", "/api/v1/auth/onboarding/password"),
    ("POST", "/api/v1/auth/onboarding/phone"),
    ("POST", "/api/v1/auth/reset/request"),
    ("POST", "/api/v1/auth/reset/security-question"),
    ("POST", "/api/v1/auth/reset/set-password"),
    ("POST", "/api/v1/auth/reset/verify-otp"),
    # Catalogue and configuration (plans, vouchers, towns/sites, branding, resellers, templates)
    ("PUT", "/api/v1/admin/branding"),
    ("POST", "/api/v1/admin/branding/logo"),
    ("POST", "/api/v1/admin/commission-rules"),
    ("DELETE", "/api/v1/admin/commission-rules/{rule_id}"),
    ("PUT", "/api/v1/admin/commission-rules/{rule_id}"),
    ("POST", "/api/v1/admin/config-templates"),
    ("DELETE", "/api/v1/admin/config-templates/{template_id}"),
    ("PUT", "/api/v1/admin/config-templates/{template_id}"),
    ("POST", "/api/v1/admin/resellers"),
    ("PUT", "/api/v1/admin/resellers/{reseller_id}"),
    ("PUT", "/api/v1/admin/resellers/{reseller_id}/topup"),
    ("POST", "/api/v1/plans"),
    ("DELETE", "/api/v1/plans/{plan_id}"),
    ("PATCH", "/api/v1/plans/{plan_id}"),
    ("PUT", "/api/v1/plans/{plan_id}"),
    ("POST", "/api/v1/plans/{plan_id}/activate"),
    ("POST", "/api/v1/plans/{plan_id}/deactivate"),
    ("PUT", "/api/v1/sites/routers/{router_id}"),
    ("POST", "/api/v1/sites/{site_id}/routers"),
    ("PUT", "/api/v1/sms-template/voucher"),
    ("POST", "/api/v1/towns"),
    ("PUT", "/api/v1/towns/{town_id}"),
    ("POST", "/api/v1/towns/{town_id}/sites"),
    ("POST", "/api/v1/vouchers/generate"),
    ("POST", "/api/v1/vouchers/print-batch/confirm"),
    ("PUT", "/api/v1/vouchers/{voucher_id}/disable"),
    ("PUT", "/api/v1/vouchers/{voucher_id}/reactivate"),
    # Routers
    ("POST", "/api/v1/admin/routers/onboard"),
    ("POST", "/api/v1/admin/routers/temp-interfaces"),
    ("POST", "/api/v1/admin/routers/test-connection"),
    ("DELETE", "/api/v1/admin/routers/{router_id}"),
    ("POST", "/api/v1/admin/routers/{router_id}/apply-template"),
    ("POST", "/api/v1/admin/routers/{router_id}/credentials"),
    ("PUT", "/api/v1/admin/routers/{router_id}/credentials"),
    ("POST", "/api/v1/admin/routers/{router_id}/disconnect-user"),
    ("POST", "/api/v1/admin/routers/{router_id}/health/check"),
    ("POST", "/api/v1/admin/routers/{router_id}/provision"),
    ("POST", "/api/v1/admin/routers/{router_id}/reboot"),
    ("POST", "/api/v1/admin/routers/{router_id}/setup/hotspot/apply"),
    ("POST", "/api/v1/admin/routers/{router_id}/setup/nat/apply"),
    ("POST", "/api/v1/admin/routers/{router_id}/setup/network/apply"),
    ("POST", "/api/v1/admin/routers/{router_id}/setup/radius/apply"),
    ("DELETE", "/api/v1/admin/routers/{router_id}/wireguard"),
    ("POST", "/api/v1/admin/routers/{router_id}/wireguard/setup"),
    # Money and credentials
    ("POST", "/api/v1/billing/invoices/{invoice_id}/pay"),
    ("DELETE", "/api/v1/payment-credentials/{provider}"),
    ("PUT", "/api/v1/payment-credentials/{provider}"),
    ("POST", "/api/v1/payment-credentials/{provider}/activate"),
    ("POST", "/api/v1/payment-credentials/{provider}/test"),
    ("POST", "/api/v1/payments/reconcile"),
    ("PUT", "/api/v1/payments/{payment_id}/refund"),
    ("PUT", "/api/v1/payments/{payment_id}/verify"),
    ("POST", "/api/v1/sms-credentials/activate-platform"),
    ("DELETE", "/api/v1/sms-credentials/{provider}"),
    ("PUT", "/api/v1/sms-credentials/{provider}"),
    ("POST", "/api/v1/sms-credentials/{provider}/activate"),
    ("POST", "/api/v1/sms-credentials/{provider}/test"),
    # Platform owner
    ("PUT", "/api/v1/platform/applications/{application_id}/approve"),
    ("PUT", "/api/v1/platform/applications/{application_id}/reject"),
    ("POST", "/api/v1/platform/auth/challenge"),
    ("POST", "/api/v1/platform/auth/login"),
    ("POST", "/api/v1/platform/auth/logout"),
    ("PUT", "/api/v1/platform/billing/default-fee"),
    ("PUT", "/api/v1/platform/invoices/{invoice_id}/waive"),
    ("PATCH", "/api/v1/platform/me"),
    ("POST", "/api/v1/platform/me/password"),
    ("POST", "/api/v1/platform/me/phone"),
    ("POST", "/api/v1/platform/me/phone/verify"),
    ("POST", "/api/v1/platform/me/security-question"),
    ("PUT", "/api/v1/platform/notification-sms-credentials"),
    ("POST", "/api/v1/platform/notification-sms-credentials/test"),
    ("PUT", "/api/v1/platform/notification-templates/{event}/{channel}"),
    ("POST", "/api/v1/platform/notification-templates/{event}/{channel}/reset"),
    ("POST", "/api/v1/platform/operators"),
    ("PATCH", "/api/v1/platform/operators/{operator_id}"),
    ("POST", "/api/v1/platform/operators/{operator_id}/admins"),
    ("POST", "/api/v1/platform/operators/{operator_id}/admins/{admin_id}/reset-password"),
    ("PUT", "/api/v1/platform/operators/{operator_id}/billing"),
    ("PUT", "/api/v1/platform/operators/{operator_id}/status"),
    ("PUT", "/api/v1/platform/payment-credentials"),
    ("POST", "/api/v1/platform/payment-credentials/test"),
    ("PATCH", "/api/v1/platform/payment-transactions/{transaction_id}/diagnostic"),
    ("PUT", "/api/v1/platform/providers/{entry_id}"),
    ("PUT", "/api/v1/platform/settings"),
    ("POST", "/api/v1/platform/setup/character-code"),
    ("POST", "/api/v1/platform/setup/complete"),
    ("POST", "/api/v1/platform/setup/phone"),
    ("POST", "/api/v1/platform/setup/phone/verify"),
    ("POST", "/api/v1/platform/setup/security-question"),
    ("PUT", "/api/v1/platform/sms-credentials"),
    ("POST", "/api/v1/platform/sms-credentials/test"),
    ("PATCH", "/api/v1/platform/sms-usage-records/{record_id}/diagnostic"),
}

# Reads that reveal a secret or bulk personal data: these must be audited too
# (P3 adds their PIN gates, P4 their rows). Listed so the guard can't forget
# them for being GETs.
SENSITIVE_READS_PENDING = {
    ("GET", "/api/v1/admin/routers/{router_id}/nas-secret"),
    ("GET", "/api/v1/payments/export"),
}


def _calls(dependant, seen):
    for dep in dependant.dependencies:
        seen.add(dep.call)
        _calls(dep, seen)
    return seen


def _routes():
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                yield method, route.path, _calls(route.dependant, set())


def _is_audited(calls) -> bool:
    return any(getattr(call, "is_audit_marker", False) for call in calls)


def _in_scope():
    found = {}
    for method, path, calls in _routes():
        if method in _READ_METHODS:
            continue
        if calls & _AUTH or path.startswith(_ANON_PREFIXES):
            found[(method, path)] = _is_audited(calls)
    return found


def test_lists_do_not_overlap():
    assert not (AUDITED & EXEMPT)
    assert not (AUDITED & PENDING)
    assert not (EXEMPT & PENDING)


def test_every_in_scope_route_is_classified():
    unclassified = sorted(set(_in_scope()) - AUDITED - EXEMPT - PENDING)
    assert not unclassified, (
        "New state-changing route(s) with no audit decision. Add "
        "Depends(audit.audited) and an audit.record(...) call, or list them "
        f"in EXEMPT with a reason: {unclassified}"
    )


def test_marker_matches_the_lists():
    scope = _in_scope()
    assert {key for key, marked in scope.items() if marked} == AUDITED


def test_no_stale_entries():
    every = {(method, path) for method, path, _ in _routes()}
    assert not (AUDITED | EXEMPT | PENDING | SENSITIVE_READS_PENDING) - every


def test_sensitive_reads_are_not_yet_marked():
    """Flips when P4 marks them: move each one into a tested set then."""
    marked = {(m, p) for m, p, calls in _routes() if _is_audited(calls)}
    assert not (SENSITIVE_READS_PENDING & marked)


def test_sign_in_and_sign_out_stay_unauthenticated():
    """The marker must not pull in an auth guard: on /auth/login and
    /auth/logout that would refuse exactly the requests they exist for."""
    for method, path, calls in _routes():
        if (method, path) in {("POST", "/api/v1/auth/login"), ("POST", "/api/v1/auth/logout")}:
            assert _is_audited(calls)
            assert not (calls & _AUTH)
