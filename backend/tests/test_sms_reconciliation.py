"""Unit tests for jobs.sms_reconciliation's two log-and-alert-only checks.

Logging is asserted against a fake logger object (recording (level, event,
kwargs) tuples) rather than caplog, since this module logs via structlog —
whether structlog routes into stdlib logging (and so into caplog) depends on
app-wide logging configuration this test file shouldn't need to know about.
Asserting directly on what the module's own `logger` was called with is
deterministic regardless of that configuration.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.jobs import sms_reconciliation
from src.modules.sms.providers.arkesel import ArkeselSMSProvider


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def all(self):
        return self._value


class FakeDb:
    def __init__(self, result_value):
        self.result_value = result_value

    async def execute(self, statement):
        return _Result(self.result_value)


@dataclass
class FakeLogger:
    events: list = field(default_factory=list)

    def info(self, event, **kwargs):
        self.events.append(("info", event, kwargs))

    def warning(self, event, **kwargs):
        self.events.append(("warning", event, kwargs))

    def error(self, event, **kwargs):
        self.events.append(("error", event, kwargs))

    def levels(self):
        return [e[0] for e in self.events]

    def events_named(self, name):
        return [e for e in self.events if e[1] == name]


@dataclass
class _FakeCredential:
    provider: str = "arkesel"
    credentials_encrypted: str = ""


_NOW = datetime(2026, 9, 12, 2, 17, tzinfo=timezone.utc)


def _patch_common(monkeypatch, *, credential, balance, settings_store, db_result):
    fake_logger = FakeLogger()
    monkeypatch.setattr(sms_reconciliation, "logger", fake_logger)

    async def _fake_get_active_credential(db):
        return credential

    monkeypatch.setattr(sms_reconciliation.platform_creds, "get_active_credential", _fake_get_active_credential)

    async def _fake_get_sms_balance(self):
        if isinstance(balance, Exception):
            raise balance
        return balance

    monkeypatch.setattr(ArkeselSMSProvider, "get_sms_balance", _fake_get_sms_balance)

    monkeypatch.setattr(sms_reconciliation, "load_credentials", lambda row: {"api_key": "k", "sender_id": "S"})

    async def _fake_get_setting(db, key):
        return settings_store.get(key, "")

    async def _fake_set_setting(db, key, value):
        settings_store[key] = value

    monkeypatch.setattr(sms_reconciliation, "get_setting", _fake_get_setting)
    monkeypatch.setattr(sms_reconciliation, "set_setting", _fake_set_setting)

    return fake_logger, FakeDb(db_result)


@pytest.mark.asyncio
async def test_no_active_credential_skips_quietly(monkeypatch):
    logger, db = _patch_common(monkeypatch, credential=None, balance=Decimal("0"), settings_store={}, db_result=0)
    await sms_reconciliation._check_balance_drift(db, now=_NOW)
    assert logger.events_named("sms_reconciliation_skipped")
    assert not logger.events_named("sms_reconciliation_drift_detected")


@pytest.mark.asyncio
async def test_first_run_establishes_baseline_without_comparing(monkeypatch):
    store = {}
    logger, db = _patch_common(
        monkeypatch, credential=_FakeCredential(), balance=Decimal("500"), settings_store=store, db_result=0
    )
    await sms_reconciliation._check_balance_drift(db, now=_NOW)
    assert logger.events_named("sms_reconciliation_baseline_established")
    assert not logger.events_named("sms_reconciliation_drift_detected")
    assert not logger.events_named("sms_reconciliation_ok")
    assert store[sms_reconciliation._LAST_BALANCE_KEY] == "500"
    assert store[sms_reconciliation._LAST_CHECKED_AT_KEY] == _NOW.isoformat()


@pytest.mark.asyncio
async def test_drift_beyond_tolerance_is_alerted(monkeypatch):
    last_checked = _NOW - timedelta(days=1)
    store = {
        sms_reconciliation._LAST_BALANCE_KEY: "100",
        sms_reconciliation._LAST_CHECKED_AT_KEY: last_checked.isoformat(),
    }
    # consumed = 100 - 90 = 10; recorded = 2 -> drift = 8, beyond the tolerance of 2.
    logger, db = _patch_common(
        monkeypatch, credential=_FakeCredential(), balance=Decimal("90"), settings_store=store, db_result=2
    )

    # Separate Arkesel accounts — the case where drift is genuinely actionable.
    # Stated explicitly: which branch this takes is now a real decision, and the
    # sibling test below covers the shared-account branch.
    async def _separate_accounts(_db):
        return False

    monkeypatch.setattr(
        sms_reconciliation, "_notifications_share_gateway_account", _separate_accounts
    )

    await sms_reconciliation._check_balance_drift(db, now=_NOW)
    drift_events = logger.events_named("sms_reconciliation_drift_detected")
    assert len(drift_events) == 1
    assert drift_events[0][2]["consumed"] == "10"
    assert drift_events[0][2]["recorded"] == "2"
    assert drift_events[0][2]["drift"] == "8"
    assert not logger.events_named("sms_reconciliation_ok")


@pytest.mark.asyncio
async def test_drift_within_tolerance_is_not_alerted(monkeypatch):
    last_checked = _NOW - timedelta(days=1)
    store = {
        sms_reconciliation._LAST_BALANCE_KEY: "100",
        sms_reconciliation._LAST_CHECKED_AT_KEY: last_checked.isoformat(),
    }
    # consumed = 100 - 91 = 9; recorded = 8 -> drift = 1, within tolerance.
    logger, db = _patch_common(
        monkeypatch, credential=_FakeCredential(), balance=Decimal("91"), settings_store=store, db_result=8
    )
    await sms_reconciliation._check_balance_drift(db, now=_NOW)
    assert not logger.events_named("sms_reconciliation_drift_detected")
    assert logger.events_named("sms_reconciliation_ok")


@pytest.mark.asyncio
async def test_topup_direction_is_not_alerted(monkeypatch):
    # Balance went UP since last check (a manual top-up) -- consumed is
    # negative, must never be treated as drift regardless of recorded usage.
    last_checked = _NOW - timedelta(days=1)
    store = {
        sms_reconciliation._LAST_BALANCE_KEY: "100",
        sms_reconciliation._LAST_CHECKED_AT_KEY: last_checked.isoformat(),
    }
    logger, db = _patch_common(
        monkeypatch, credential=_FakeCredential(), balance=Decimal("500"), settings_store=store, db_result=0
    )
    await sms_reconciliation._check_balance_drift(db, now=_NOW)
    assert not logger.events_named("sms_reconciliation_drift_detected")
    assert logger.events_named("sms_reconciliation_ok")


@pytest.mark.asyncio
async def test_balance_fetch_failure_logs_and_does_not_update_checkpoint(monkeypatch):
    store = {"existing": "untouched"}
    logger, db = _patch_common(
        monkeypatch, credential=_FakeCredential(), balance=RuntimeError("network down"),
        settings_store=store, db_result=0,
    )
    await sms_reconciliation._check_balance_drift(db, now=_NOW)
    assert logger.events_named("sms_reconciliation_balance_fetch_failed")
    assert sms_reconciliation._LAST_BALANCE_KEY not in store  # never set on failure


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shares, expect_event",
    [
        # Sharing the gateway's account: drift is expected and proportional to
        # notification volume, so it is reported without alerting.
        (True, "sms_reconciliation_drift_expected_shared_account"),
        # Separate account: drift means what it used to mean.
        (False, "sms_reconciliation_drift_detected"),
    ],
)
async def test_drift_alerting_depends_on_whether_notifications_share_the_account(
    monkeypatch, shares, expect_event
):
    last_checked = _NOW - timedelta(days=1)
    store = {
        sms_reconciliation._LAST_BALANCE_KEY: "100",
        sms_reconciliation._LAST_CHECKED_AT_KEY: last_checked.isoformat(),
    }
    # consumed = 10, recorded = 2 -> drift 8, beyond the tolerance of 2.
    logger, db = _patch_common(
        monkeypatch, credential=_FakeCredential(), balance=Decimal("90"),
        settings_store=store, db_result=2,
    )

    async def _fake_shares(_db):
        return shares

    monkeypatch.setattr(sms_reconciliation, "_notifications_share_gateway_account", _fake_shares)

    await sms_reconciliation._check_balance_drift(db, now=_NOW)

    assert logger.events_named(expect_event), f"expected {expect_event}"
    other = (
        "sms_reconciliation_drift_detected" if shares
        else "sms_reconciliation_drift_expected_shared_account"
    )
    assert not logger.events_named(other), f"should not have logged {other}"


@pytest.mark.asyncio
async def test_no_stale_usage_logs_nothing(monkeypatch):
    fake_logger = FakeLogger()
    monkeypatch.setattr(sms_reconciliation, "logger", fake_logger)
    db = FakeDb([])
    await sms_reconciliation._check_stale_unbilled_usage(db, now=_NOW)
    assert fake_logger.events == []


@pytest.mark.asyncio
async def test_stale_usage_warns_per_operator(monkeypatch):
    fake_logger = FakeLogger()
    monkeypatch.setattr(sms_reconciliation, "logger", fake_logger)
    rows = [
        ("op-1", 12, Decimal("3.60"), _NOW - timedelta(days=40)),
        ("op-2", 3, Decimal("0.90"), _NOW - timedelta(days=50)),
    ]
    db = FakeDb(rows)
    await sms_reconciliation._check_stale_unbilled_usage(db, now=_NOW)
    warnings = fake_logger.events_named("sms_reconciliation_stale_unbilled_usage")
    assert len(warnings) == 2
    assert {w[2]["operator"] for w in warnings} == {"op-1", "op-2"}
