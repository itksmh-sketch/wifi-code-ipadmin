"""Unit tests for the platform_name setting.

Covers: get_platform_name's fallbacks (settings row, then SENDGRID_FROM_NAME,
then "IpAdmin") and that it never raises; the notification dispatcher, the
admin account/security SMS and the Templates preview all using it instead of
a hardcoded name; the preview's segment check measuring the real name; and the
settings endpoint's validation.

No server and no database: sessions and senders are faked. Safe to run
anywhere, including the production container.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.db.base as db_base
from src.db.base import get_db
from src.middleware.auth import get_platform_owner_context
from src.modules.admin_accounts import notifications as admin_sms
from src.modules.notifications import dispatcher
from src.modules.notifications import template_catalog as catalog
from src.modules.platform import routes as platform_routes
from src.modules.platform import settings_service
from src.modules.sms.types import SMSSendResult


# ── get_platform_name ─────────────────────────────────────────────────────────

class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def lookup(monkeypatch):
    """Control the row get_setting reads, and the config fallback."""
    state = {"row": None, "config": "IpAdmin"}

    async def fake_execute(statement):
        if isinstance(state["row"], Exception):
            raise state["row"]
        return SimpleNamespace(scalar_one_or_none=lambda: state["row"])

    session = _FakeSession()
    session.execute = fake_execute
    monkeypatch.setattr(db_base, "async_session_factory", lambda: session)
    monkeypatch.setattr(settings_service, "settings", SimpleNamespace(sendgrid_from_name=state["config"]))

    def set_config(value):
        monkeypatch.setattr(settings_service, "settings", SimpleNamespace(sendgrid_from_name=value))

    state["set_config"] = set_config
    return state


def test_setting_falls_back_to_the_email_sender_name():
    assert settings_service.PLATFORM_SETTING_KEYS["platform_name"] == "sendgrid_from_name"


@pytest.mark.asyncio
async def test_stored_name_wins(lookup):
    lookup["row"] = "Kasoa WiFi Hub"
    assert await settings_service.get_platform_name() == "Kasoa WiFi Hub"


@pytest.mark.asyncio
async def test_unset_row_uses_sender_name(lookup):
    lookup["set_config"]("IpAdmin")
    assert await settings_service.get_platform_name() == "IpAdmin"


@pytest.mark.asyncio
async def test_lookup_failure_uses_sender_name_and_does_not_raise(lookup):
    lookup["row"] = RuntimeError("connection refused")
    lookup["set_config"]("IpAdmin")
    assert await settings_service.get_platform_name() == "IpAdmin"


@pytest.mark.asyncio
async def test_nothing_configured_anywhere_gives_ipadmin(lookup):
    lookup["row"] = RuntimeError("connection refused")
    lookup["set_config"]("")
    assert await settings_service.get_platform_name() == "IpAdmin"


# ── dispatcher: every operator notification ───────────────────────────────────

@pytest.mark.asyncio
async def test_notifications_carry_the_configured_name(monkeypatch):
    rendered = []

    async def name():
        return "Kasoa WiFi Hub"

    async def render_email(event, values):
        rendered.append(values)
        return "s", "h", "t"

    async def render_sms(event, values):
        rendered.append(values)
        return "t"

    async def noop(*args, **kwargs):
        pass

    async def support():
        return "help@isp.test"

    monkeypatch.setattr(dispatcher, "get_platform_name", name)
    monkeypatch.setattr(dispatcher, "_support_email", support)
    monkeypatch.setattr(dispatcher.store, "render_email", render_email)
    monkeypatch.setattr(dispatcher.store, "render_sms", render_sms)
    monkeypatch.setattr(dispatcher, "_send_email", noop)
    monkeypatch.setattr(dispatcher, "_send_sms", noop)

    await dispatcher.notify_application_received(email="a@b.test", contact_name="K", isp_name="I", phone="1")
    await dispatcher.notify_application_approved(
        email="a@b.test", phone="1", contact_name="K", isp_name="I",
        admin_email="a@b.test", temp_password="x", trial_days=14,
    )
    await dispatcher.notify_trial_expired(email="a@b.test", phone="1", isp_name="I")
    await dispatcher.notify_suspended(email="a@b.test", phone="1", isp_name="I")

    assert rendered and {v["platform_name"] for v in rendered} == {"Kasoa WiFi Hub"}


def test_dispatcher_no_longer_uses_the_placeholder_constant():
    assert not hasattr(dispatcher, "DEFAULT_PLATFORM_NAME")


# ── admin account / security SMS ──────────────────────────────────────────────

@pytest.fixture
def admin_sent(monkeypatch):
    sent = []

    async def name():
        return "Kasoa WiFi Hub"

    async def login_url():
        return "https://example.test/admin/login"

    async def fake_send(to, message, *, kind):
        sent.append(message)
        return SMSSendResult(success=True)

    monkeypatch.setattr(admin_sms, "get_platform_name", name)
    monkeypatch.setattr(admin_sms, "_login_url", login_url)
    monkeypatch.setattr(admin_sms, "_send", fake_send)
    return sent


@pytest.mark.asyncio
async def test_admin_sms_use_the_configured_name(admin_sent):
    admin = SimpleNamespace(email="a@b.test", phone="+233244123456", phone_verified=True)
    await admin_sms.send_temp_password_sms(admin, "Temp1234")
    await admin_sms.send_temp_password_sms(admin, "Temp1234", reason="reset")
    await admin_sms.send_otp_sms("+233244123456", "123456", purpose="reset")
    await admin_sms.send_lockout_sms(admin, kind="login")
    await admin_sms.send_lockout_sms(admin, kind="pin")
    await admin_sms.send_phone_changed_sms("+233200000000", admin)

    assert len(admin_sent) == 6
    for message in admin_sent:
        assert message.startswith("Kasoa WiFi Hub")
        assert "IpAdmin" not in message


# ── Templates preview and segment check ───────────────────────────────────────

def test_preview_shows_the_configured_name():
    default = catalog.DEFAULTS[("application_approved", catalog.SMS)]
    shown = catalog.preview(
        "application_approved", catalog.SMS, subject=None,
        body_text=default.body_text, body_html=None, platform_name="Kasoa WiFi Hub",
    )
    assert shown["body_text"].startswith("Welcome to Kasoa WiFi Hub!")
    assert catalog.DEFAULT_PLATFORM_NAME not in shown["body_text"]


def test_segment_check_measures_the_real_name():
    # Pad a template to sit exactly at the segment cap with a short name; a
    # longer configured name then has to push it over.
    short, long = "Ab", "W" * settings_service.PLATFORM_NAME_MAX_LENGTH
    body = "{platform_name} {isp_name} {login_url} {admin_email} {temp_password} "
    base = catalog.preview("application_approved", catalog.SMS, subject=None,
                           body_text=body, body_html=None, platform_name=short)
    # Multipart GSM-7 segments carry 153 characters each.
    body += "x" * (153 * catalog.MAX_SMS_SEGMENTS - base["character_count"])
    ok = catalog.validation_error("application_approved", catalog.SMS, subject=None,
                                  body_text=body, body_html=None, platform_name=short)
    too_long = catalog.validation_error("application_approved", catalog.SMS, subject=None,
                                        body_text=body, body_html=None, platform_name=long)
    assert ok is None
    assert too_long and too_long.startswith("Too long")


@pytest.mark.parametrize("event", [e for (e, ch) in catalog.DEFAULTS if ch == catalog.SMS])
def test_every_default_sms_fits_with_the_longest_allowed_name(event):
    default = catalog.DEFAULTS[(event, catalog.SMS)]
    assert catalog.validation_error(
        event, catalog.SMS, subject=None, body_text=default.body_text, body_html=None,
        platform_name="W" * settings_service.PLATFORM_NAME_MAX_LENGTH,
    ) is None


# ── PUT /platform/settings ────────────────────────────────────────────────────

@pytest.fixture
def settings_api(monkeypatch):
    written = {}

    async def fake_set_setting(db, key, value):
        written[key] = value

    async def fake_get_all_settings(db):
        return dict(written)

    class _Db:
        async def commit(self):
            pass

    async def fake_get_db():
        yield _Db()

    monkeypatch.setattr(settings_service, "set_setting", fake_set_setting)
    monkeypatch.setattr(settings_service, "get_all_settings", fake_get_all_settings)
    app = FastAPI()
    app.include_router(platform_routes.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_platform_owner_context] = lambda: SimpleNamespace(id=uuid.uuid4())
    return SimpleNamespace(client=TestClient(app), written=written)


@pytest.mark.parametrize("raw, saved", [("IpAdmin", "IpAdmin"), ("  Kasoa   WiFi  Hub ", "Kasoa WiFi Hub")])
def test_settings_accepts_and_tidies_the_name(settings_api, raw, saved):
    res = settings_api.client.put("/api/v1/platform/settings", json={"platform_name": raw})
    assert res.status_code == 200
    assert settings_api.written == {"platform_name": saved}


@pytest.mark.parametrize("raw", ["", "   ", "W" * 31])
def test_settings_refuses_empty_or_long_name_and_writes_nothing(settings_api, raw):
    res = settings_api.client.put(
        "/api/v1/platform/settings", json={"platform_support_email": "help@isp.test", "platform_name": raw}
    )
    assert res.status_code == 400
    assert "Platform name" in res.json()["detail"]
    assert settings_api.written == {}
