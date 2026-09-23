"""Unit tests for the platform_trial_days setting.

Covers: get_trial_days' resolution order (settings row, then TRIAL_DAYS, then
14) and its fallback when the lookup fails; approve_application stamping
trial_ends_at, the trial_started event and the approval email's {trial_days}
from one value; and the settings endpoint refusing anything but 1-365 days.

No server and no database: sessions and collaborators are faked. Safe to run
anywhere, including the production container.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.db.base as db_base
from src.db.base import get_db
from src.db.models import OperatorBillingEvent
from src.middleware.auth import get_platform_owner_context
from src.modules.applications import service
from src.modules.platform import routes as platform_routes
from src.modules.platform import settings_service
from src.modules.sms.types import SMSSendResult


# ── get_trial_days ─────────────────────────────────────────────────────────────

class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def stored(monkeypatch):
    """Set what get_setting returns for platform_trial_days (or an exception)."""
    state = {"value": None, "keys": []}

    async def fake_get_setting(db, key):
        state["keys"].append(key)
        if isinstance(state["value"], Exception):
            raise state["value"]
        return state["value"]

    monkeypatch.setattr(db_base, "async_session_factory", lambda: _FakeSession())
    monkeypatch.setattr(settings_service, "get_setting", fake_get_setting)
    return state


def _config_trial_days(monkeypatch, value):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(trial_days=value))


def test_setting_is_registered_with_trial_days_config_fallback():
    assert settings_service.PLATFORM_SETTING_KEYS["platform_trial_days"] == "trial_days"


@pytest.mark.asyncio
async def test_stored_setting_wins(stored, monkeypatch):
    _config_trial_days(monkeypatch, 14)
    stored["value"] = "30"
    assert await service.get_trial_days() == 30
    assert stored["keys"] == ["platform_trial_days"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["abc", "0", "-5", "366", "1.5"])
async def test_invalid_stored_value_falls_back_to_config(stored, monkeypatch, bad):
    _config_trial_days(monkeypatch, 21)
    stored["value"] = bad
    assert await service.get_trial_days() == 21


@pytest.mark.asyncio
async def test_lookup_failure_falls_back_to_config(stored, monkeypatch):
    _config_trial_days(monkeypatch, 21)
    stored["value"] = RuntimeError("connection refused")
    assert await service.get_trial_days() == 21


@pytest.mark.asyncio
async def test_invalid_config_falls_back_to_14(stored, monkeypatch):
    _config_trial_days(monkeypatch, 0)
    stored["value"] = RuntimeError("connection refused")
    assert await service.get_trial_days() == 14


# ── approve_application ───────────────────────────────────────────────────────

class _ApproveDb:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        pass

    async def refresh(self, obj):
        pass


@pytest.mark.asyncio
async def test_approval_stamps_one_trial_length_everywhere(monkeypatch):
    emails = []

    async def trial_days():
        return 30

    async def fee(db):
        return Decimal("200.00")

    async def slug(db, base):
        return base

    async def provision(db, **kwargs):
        return SimpleNamespace(phone=kwargs["phone"]), "TempPass1"

    async def sms(admin, temp_password):
        return SMSSendResult(success=True)

    async def notify_approved(**kwargs):
        emails.append(kwargs)

    monkeypatch.setattr(service, "get_trial_days", trial_days)
    monkeypatch.setattr(service, "get_default_monthly_fee", fee)
    monkeypatch.setattr(service, "_unique_slug", slug)
    monkeypatch.setattr(service, "provision_operator_admin", provision)
    monkeypatch.setattr(service, "send_temp_password_sms", sms)
    monkeypatch.setattr(service.notify, "notify_application_approved", notify_approved)

    app = SimpleNamespace(
        isp_name="AccraNet ISP", email="kwame@accranet.test", phone="+233244123456",
        contact_name="Kwame Mensah", status="pending",
    )
    before = datetime.now(timezone.utc)
    db = _ApproveDb()
    operator, _, _ = await service.approve_application(db, app, uuid.uuid4())
    after = datetime.now(timezone.utc)

    assert before + timedelta(days=30) <= operator.trial_ends_at <= after + timedelta(days=30)
    event = next(o for o in db.added if isinstance(o, OperatorBillingEvent))
    assert event.event_metadata["trial_days"] == 30
    assert emails[0]["trial_days"] == 30


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


@pytest.mark.parametrize("raw, saved", [("30", "30"), (" 7 ", "7"), ("030", "30"), ("1", "1"), ("365", "365")])
def test_settings_accepts_whole_days_in_range(settings_api, raw, saved):
    res = settings_api.client.put("/api/v1/platform/settings", json={"platform_trial_days": raw})
    assert res.status_code == 200
    assert settings_api.written == {"platform_trial_days": saved}


@pytest.mark.parametrize("raw", ["", "0", "366", "-1", "1.5", "abc", "14 days", "²"])
def test_settings_refuses_bad_trial_length_and_writes_nothing(settings_api, raw):
    res = settings_api.client.put(
        "/api/v1/platform/settings",
        json={"platform_support_email": "help@isp.test", "platform_trial_days": raw},
    )
    assert res.status_code == 400
    assert "between 1 and 365" in res.json()["detail"]
    assert settings_api.written == {}
