"""The per-operator webhook URL: built on the platform_settings row
(webhook_base_url), falling back to config only when the row is empty or the
lookup fails, and the two places that use it (the operator's Payments page and
the callback URL handed to the payment provider).

Pure unit tests: the settings lookup and the database are faked. No server,
no DB, no Redis.
"""
from __future__ import annotations

import types
import uuid

import pytest

from src.config import get_settings
from src.db.models import OperatorPaymentCredential, OperatorSMSCredential
from src.modules.credentials import service as credentials_service
from src.modules.payments import service as payments_service
from src.modules.platform import settings_service
from src.modules.webhooks import urls

DOMAIN = "https://ip-admin.example.test"
ENV_BASE = "http://203.0.113.10"


@pytest.fixture
def env_base(monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_base_url", ENV_BASE)
    # settings_service binds its own reference to the same cached Settings.
    monkeypatch.setattr(settings_service.settings, "webhook_base_url", ENV_BASE)
    return ENV_BASE


class _Session:
    """async_session_factory() stand-in; the row value is what the table holds."""

    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        value = self.value
        return types.SimpleNamespace(scalar_one_or_none=lambda: value)


def _table(monkeypatch, value):
    monkeypatch.setattr(urls, "async_session_factory", lambda: _Session(value))


# ── build_webhook_url ─────────────────────────────────────────────────────


def test_url_shape():
    assert urls.build_webhook_url(DOMAIN, "paystack", "we-active") == f"{DOMAIN}/api/v1/webhooks/paystack/we-active"


def test_trailing_slash_and_whitespace_on_the_base_are_dropped():
    assert urls.build_webhook_url(f" {DOMAIN}/ ", "flutterwave", "op") == f"{DOMAIN}/api/v1/webhooks/flutterwave/op"


@pytest.mark.parametrize("base,slug", [("", "op"), ("  ", "op"), (DOMAIN, None), (DOMAIN, "")])
def test_no_url_without_a_base_and_a_slug(base, slug):
    assert urls.build_webhook_url(base, "paystack", slug) is None


# ── webhook_base_url: the table first, config as the fallback ─────────────


@pytest.mark.asyncio
async def test_the_table_value_wins_over_config(monkeypatch, env_base):
    _table(monkeypatch, DOMAIN + "/")
    assert await urls.webhook_base_url() == DOMAIN


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, ""])
async def test_an_absent_or_empty_row_falls_back_to_config(monkeypatch, env_base, stored):
    _table(monkeypatch, stored)
    assert await urls.webhook_base_url() == ENV_BASE


@pytest.mark.asyncio
async def test_a_failed_lookup_falls_back_to_config(monkeypatch, env_base):
    def broken():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(urls, "async_session_factory", broken)
    assert await urls.webhook_base_url() == ENV_BASE


@pytest.mark.asyncio
async def test_nothing_configured_anywhere_means_no_url(monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_base_url", "")
    monkeypatch.setattr(settings_service.settings, "webhook_base_url", "")
    _table(monkeypatch, None)
    assert await urls.webhook_base_url() == ""


# ── The two callers ───────────────────────────────────────────────────────


class _ViewDb:
    """Just enough AsyncSession for credentials.service.build_view."""

    def __init__(self, rows, operator):
        self.results = [rows, []]  # credential rows, then catalog entries
        self.operator = operator

    async def execute(self, _stmt):
        items = self.results.pop(0)
        return types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: items))

    async def get(self, _model, _id):
        return self.operator


def _credential_row(provider="paystack"):
    return types.SimpleNamespace(provider=provider, is_active=True, last_validated_at=None, last_validation_error=None)


@pytest.mark.asyncio
async def test_the_payments_page_shows_the_https_url_from_the_table(monkeypatch, env_base):
    _table(monkeypatch, DOMAIN)
    monkeypatch.setattr(credentials_service, "load_credentials", lambda row: {})
    db = _ViewDb([_credential_row()], types.SimpleNamespace(slug="we-active"))

    view = await credentials_service.build_view(db, category="payment", model=OperatorPaymentCredential, operator_id=uuid.uuid4())

    assert [c.webhook_url for c in view.configured] == [f"{DOMAIN}/api/v1/webhooks/paystack/we-active"]


@pytest.mark.asyncio
async def test_sms_credentials_get_no_webhook_url_and_no_lookup(monkeypatch, env_base):
    async def must_not_run():
        raise AssertionError("an SMS view has no webhook URL to build")

    monkeypatch.setattr(credentials_service, "webhook_base_url", must_not_run)
    monkeypatch.setattr(credentials_service, "load_credentials", lambda row: {})
    db = _ViewDb([_credential_row("arkesel")], types.SimpleNamespace(slug="we-active"))

    view = await credentials_service.build_view(db, category="sms", model=OperatorSMSCredential, operator_id=uuid.uuid4())

    assert [c.webhook_url for c in view.configured] == [None]


@pytest.mark.asyncio
async def test_the_provider_callback_url_is_built_on_the_table_value(monkeypatch, env_base):
    _table(monkeypatch, DOMAIN)
    built = {}

    async def resolve(_db, _operator_id):
        return "paystack", {"public_key": "pk_test", "secret_key": "sk_test"}

    def build(provider_key, credentials, *, callback_url=None):
        built.update(provider_key=provider_key, callback_url=callback_url)
        return object()

    monkeypatch.setattr(payments_service, "resolve_active_payment_provider", resolve)
    monkeypatch.setattr(payments_service, "build_payment_provider", build)

    class Db:
        async def get(self, _model, _id):
            return types.SimpleNamespace(slug="we-active")

    tx = types.SimpleNamespace(isp_operator_id=uuid.uuid4())
    await payments_service.PaymentService().provider_for_transaction(Db(), tx)

    assert built == {"provider_key": "paystack", "callback_url": f"{DOMAIN}/api/v1/webhooks/paystack/we-active"}
