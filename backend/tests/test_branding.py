"""Operator branding: pure default-resolution + public endpoint logic (no stack
needed) and tenant-isolation integration tests (require the live API + DB)."""
import asyncio
import urllib.request
import uuid
from types import SimpleNamespace

import pytest

from test_multi_tenant_security import BASE_URL, _request
from test_multi_tenancy import _create_operator_pair


def _server_up() -> bool:
    try:
        urllib.request.urlopen(f"{BASE_URL}/api/v1/health", timeout=2)
        return True
    except Exception:
        return False


# Integration tests below need the live API + DB; skip cleanly when it isn't up.
requires_server = pytest.mark.skipif(not _server_up(), reason="live API + DB not running")

from src.modules.branding.service import (
    build_branding,
    DEFAULT_ACCENT_COLOR,
    DEFAULT_BACKGROUND_GRADIENT_START,
    DEFAULT_PRIMARY_COLOR,
    DEFAULT_WELCOME_MESSAGE,
)
from src.portal.routes import portal_branding
from src.utils.portal_token import create_portal_router_token


def _operator(**overrides):
    base = dict(
        name="Acme ISP",
        portal_display_name=None,
        logo_url=None,
        primary_color=None,
        accent_color=None,
        background_gradient_start=None,
        portal_welcome_message=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ─── Pure default resolution (runs without a DB) ────────────────────────────


def test_build_branding_applies_defaults_for_unset_fields():
    b = build_branding(_operator())
    assert b.primary_color == DEFAULT_PRIMARY_COLOR == "#2563eb"
    assert b.accent_color == DEFAULT_ACCENT_COLOR == "#764ba2"
    assert b.background_gradient_start == DEFAULT_BACKGROUND_GRADIENT_START == "#667eea"
    assert b.welcome_message == DEFAULT_WELCOME_MESSAGE
    assert b.logo_url is None
    # display name falls back to the operator's name when not customised
    assert b.portal_display_name == "Acme ISP"


def test_build_branding_keeps_configured_values():
    b = build_branding(_operator(
        portal_display_name="Acme WiFi",
        logo_url="/portal/statics/branding/logo_1_123.png",
        primary_color="#112233",
        accent_color="#445566",
        background_gradient_start="#778899",
        portal_welcome_message="Welcome aboard",
    ))
    assert b.portal_display_name == "Acme WiFi"
    assert b.logo_url == "/portal/statics/branding/logo_1_123.png"
    assert b.primary_color == "#112233"
    assert b.accent_color == "#445566"
    assert b.background_gradient_start == "#778899"
    assert b.welcome_message == "Welcome aboard"


def test_build_branding_none_operator_is_platform_defaults():
    b = build_branding(None)
    assert b.portal_display_name is None
    assert b.logo_url is None
    assert b.primary_color == DEFAULT_PRIMARY_COLOR
    assert b.accent_color == DEFAULT_ACCENT_COLOR
    assert b.background_gradient_start == DEFAULT_BACKGROUND_GRADIENT_START
    assert b.welcome_message == DEFAULT_WELCOME_MESSAGE


# ─── Public /portal/branding/{rt} resolution + graceful fallback ────────────


class _Result:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeDB:
    """Returns queued rows in order, one per execute() call (Router then Operator)."""
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, *args, **kwargs):
        return _Result(self._results.pop(0) if self._results else None)


def test_portal_branding_resolves_operator_from_valid_token():
    operator_id = uuid.uuid4()
    router_row = SimpleNamespace(isp_operator_id=operator_id, site_id=uuid.uuid4())
    operator = _operator(portal_display_name="Acme WiFi", primary_color="#abcdef")
    db = _FakeDB([router_row, operator])  # 1st execute: router lookup, 2nd: operator
    rt = create_portal_router_token(str(uuid.uuid4()))
    res = asyncio.run(portal_branding(rt, db))
    assert res.portal_display_name == "Acme WiFi"
    assert res.primary_color == "#abcdef"
    assert res.accent_color == DEFAULT_ACCENT_COLOR  # default fills the unset field


def test_portal_branding_bad_or_missing_token_returns_platform_defaults():
    # Garbage token: never reaches the DB, never errors.
    res = asyncio.run(portal_branding("not-a-token", _FakeDB([])))
    assert res.portal_display_name is None
    assert res.primary_color == DEFAULT_PRIMARY_COLOR
    assert res.accent_color == DEFAULT_ACCENT_COLOR
    assert res.welcome_message == DEFAULT_WELCOME_MESSAGE


def test_portal_branding_valid_token_unknown_router_returns_defaults():
    # Properly signed token but the router no longer exists -> defaults, no error.
    rt = create_portal_router_token(str(uuid.uuid4()))
    res = asyncio.run(portal_branding(rt, _FakeDB([None])))  # router lookup -> None
    assert res.primary_color == DEFAULT_PRIMARY_COLOR
    assert res.portal_display_name is None


# ─── Tenant isolation + endpoint behaviour (require the running API + DB) ────


@requires_server
def test_branding_requires_auth():
    status, _ = _request("GET", "/api/v1/admin/branding")
    assert status in (401, 403)


@requires_server
def test_branding_get_returns_defaults_when_unset():
    _, _, admin_a, _ = _create_operator_pair("phase2a-defaults")
    status, body = _request("GET", "/api/v1/admin/branding", token=admin_a)
    assert status == 200, body
    assert body["primary_color"] == "#2563eb"
    assert body["accent_color"] == "#764ba2"
    assert body["background_gradient_start"] == "#667eea"
    assert body["welcome_message"] == "Enter your voucher code to get online"
    assert body["logo_url"] is None


@requires_server
def test_branding_is_tenant_isolated():
    _, _, admin_a, admin_b = _create_operator_pair("phase2a-brand")
    status, body = _request(
        "PUT", "/api/v1/admin/branding", token=admin_a,
        body={"primary_color": "#abcdef", "portal_display_name": "A WiFi"},
    )
    assert status == 200, body

    # Operator B sees only its own (default) branding, never A's.
    status, body_b = _request("GET", "/api/v1/admin/branding", token=admin_b)
    assert status == 200, body_b
    assert body_b["primary_color"] == "#2563eb"
    assert body_b["portal_display_name"] != "A WiFi"

    # Operator B cannot overwrite into A: B's PUT only changes B.
    status, _ = _request("PUT", "/api/v1/admin/branding", token=admin_b, body={"primary_color": "#000000"})
    assert status == 200
    status, body_a = _request("GET", "/api/v1/admin/branding", token=admin_a)
    assert body_a["primary_color"] == "#abcdef"
    assert body_a["portal_display_name"] == "A WiFi"


@requires_server
def test_branding_rejects_invalid_hex_color():
    _, _, admin_a, _ = _create_operator_pair("phase2a-badhex")
    status, _ = _request("PUT", "/api/v1/admin/branding", token=admin_a, body={"primary_color": "red"})
    assert status == 422


@requires_server
def test_portal_branding_endpoint_falls_back_live():
    # Unauthenticated, bad token -> platform defaults, HTTP 200 (never errors).
    status, body = _request("GET", "/portal/branding/not-a-real-token")
    assert status == 200, body
    assert body["primary_color"] == "#2563eb"
    assert body["accent_color"] == "#764ba2"
    assert body["background_gradient_start"] == "#667eea"
