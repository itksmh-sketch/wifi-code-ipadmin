"""Unit tests for the platform-provided SMS gateway's backend wiring: the
resolve_active_sms_provider branch, the build_sms_provider registry case, rate
resolution's fail-closed behaviour, and the metering write's retry/idempotency
contract. No server, no real DB — FakeDb queues results the same way
test_payment_service.py does.
"""
from dataclasses import dataclass
from decimal import Decimal

import pytest

from src.modules.credentials.service import dump_credentials
from src.modules.platform.platform_sms_rate import (
    PlatformSMSRateNotConfigured,
    get_current_platform_sms_rate,
)
from src.modules.sms.provider_resolver import resolve_active_sms_provider
from src.modules.sms.providers.arkesel import ArkeselSMSProvider
from src.modules.sms.providers.registry import build_sms_provider


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeDb:
    def __init__(self, results):
        self.results = list(results)

    async def execute(self, statement):
        return _ScalarResult(self.results.pop(0))


@dataclass
class _FakeOperatorSMSCredentialRow:
    provider: str
    credentials_encrypted: str


@dataclass
class _FakePlatformSMSCredentialRow:
    provider: str
    credentials_encrypted: str


@dataclass
class _FakeCatalogEntry:
    platform_rate_per_segment: Decimal | None


# --- resolve_active_sms_provider ---

@pytest.mark.asyncio
async def test_resolve_bring_your_own_unchanged():
    row = _FakeOperatorSMSCredentialRow(
        provider="arkesel", credentials_encrypted=dump_credentials({"api_key": "k", "from": "SENDER"})
    )
    db = FakeDb([row])
    result = await resolve_active_sms_provider(db, "op-1")
    assert result == ("arkesel", {"api_key": "k", "from": "SENDER"})


@pytest.mark.asyncio
async def test_resolve_platform_gateway_configured():
    operator_row = _FakeOperatorSMSCredentialRow(
        provider="arkesel_platform", credentials_encrypted=dump_credentials({})
    )
    platform_row = _FakePlatformSMSCredentialRow(
        provider="arkesel",
        credentials_encrypted=dump_credentials({"api_key": "platform-key", "sender_id": "PLATFORM"}),
    )
    db = FakeDb([operator_row, platform_row])
    result = await resolve_active_sms_provider(db, "op-1")
    assert result == ("arkesel_platform", {"api_key": "platform-key", "sender_id": "PLATFORM"})


@pytest.mark.asyncio
async def test_resolve_platform_gateway_selected_but_not_configured_fails_closed():
    operator_row = _FakeOperatorSMSCredentialRow(
        provider="arkesel_platform", credentials_encrypted=dump_credentials({})
    )
    db = FakeDb([operator_row, None])  # platform_sms_credentials lookup finds nothing
    result = await resolve_active_sms_provider(db, "op-1")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_no_active_provider_returns_none():
    db = FakeDb([None])
    result = await resolve_active_sms_provider(db, "op-1")
    assert result is None


# --- build_sms_provider registry ---

def test_registry_builds_arkesel_platform_from_platform_field_names():
    provider = build_sms_provider("arkesel_platform", {"api_key": "k", "sender_id": "SENDER"})
    assert isinstance(provider, ArkeselSMSProvider)


def test_registry_unknown_provider_raises():
    with pytest.raises(ValueError):
        build_sms_provider("not-a-real-provider", {})


# --- get_current_platform_sms_rate ---

@pytest.mark.asyncio
async def test_rate_resolution_raises_when_no_catalog_row():
    db = FakeDb([None])
    with pytest.raises(PlatformSMSRateNotConfigured):
        await get_current_platform_sms_rate(db)


@pytest.mark.asyncio
async def test_rate_resolution_raises_when_rate_unset():
    db = FakeDb([_FakeCatalogEntry(platform_rate_per_segment=None)])
    with pytest.raises(PlatformSMSRateNotConfigured):
        await get_current_platform_sms_rate(db)


@pytest.mark.asyncio
async def test_rate_resolution_returns_configured_rate():
    db = FakeDb([_FakeCatalogEntry(platform_rate_per_segment=Decimal("0.0350"))])
    rate = await get_current_platform_sms_rate(db)
    assert rate == Decimal("0.0350")


# --- record_platform_sms_usage: retry + idempotency contract ---

class _FakeMeteringSession:
    """Simulates async_session_factory()'s context manager. `behavior` is a
    list of "ok"/"fail" popped one per attempt, so a test can script exactly
    when a transient failure recovers."""

    def __init__(self, behavior):
        self.behavior = behavior

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement):
        return None

    async def commit(self):
        if self.behavior.pop(0) == "fail":
            raise RuntimeError("simulated transient DB failure")


@pytest.mark.asyncio
async def test_metering_write_succeeds_first_try(monkeypatch):
    from src.modules.sms import metering

    attempts = ["ok"]
    monkeypatch.setattr(metering, "async_session_factory", lambda: _FakeMeteringSession(attempts))

    ok = await metering.record_platform_sms_usage(
        isp_operator_id="op-1",
        provider_reference="ref-1",
        segment_count=1,
        rate_ghs_per_segment=Decimal("0.03"),
        amount_ghs=Decimal("0.03"),
    )
    assert ok is True


@pytest.mark.asyncio
async def test_metering_write_recovers_after_transient_failure(monkeypatch):
    from src.modules.sms import metering

    attempts = ["fail", "ok"]
    monkeypatch.setattr(metering, "async_session_factory", lambda: _FakeMeteringSession(attempts))
    monkeypatch.setattr(metering.asyncio, "sleep", lambda *_a, **_k: _noop())

    ok = await metering.record_platform_sms_usage(
        isp_operator_id="op-1",
        provider_reference="ref-1",
        segment_count=1,
        rate_ghs_per_segment=Decimal("0.03"),
        amount_ghs=Decimal("0.03"),
    )
    assert ok is True


@pytest.mark.asyncio
async def test_metering_write_exhausts_retries_and_logs(monkeypatch, caplog):
    from src.modules.sms import metering

    attempts = ["fail", "fail", "fail"]
    monkeypatch.setattr(metering, "async_session_factory", lambda: _FakeMeteringSession(attempts))
    monkeypatch.setattr(metering.asyncio, "sleep", lambda *_a, **_k: _noop())

    with caplog.at_level("ERROR"):
        ok = await metering.record_platform_sms_usage(
            isp_operator_id="op-1",
            provider_reference="ref-1",
            segment_count=1,
            rate_ghs_per_segment=Decimal("0.03"),
            amount_ghs=Decimal("0.03"),
        )
    assert ok is False
    assert "sms_usage_record_write_failed" in caplog.text


async def _noop():
    return None
