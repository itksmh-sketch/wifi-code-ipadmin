"""Unit tests for the captive portal's wrong-code limits (src/portal/login_limits.py).

Covers: MAC parsing; the per-device / no-MAC / per-IP keys; the 429 wording
and wait; counting in one MULTI with EXPIRE NX; an alert only when a limit is
reached, deduped; Redis faults failing open with a throttled log and one
platform-owner alert an hour; the limits read from platform settings with a
fallback to config; and the settings endpoint refusing bad limits.

Fake Redis and fake sessions only: no server, no database. Safe to run
anywhere. tests/test_portal_login_limits_flow.py runs the same paths on a real
database and a real Redis.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from src.portal import login_limits
from src.portal.login_limits import Attempt, Limits

LIMITS = Limits(device=5, ip=30, operator_alert=50)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.queued = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.queued.append((name, args, kwargs))
        return queue

    async def execute(self):
        if self.redis.fail:
            raise self.redis.fail
        self.redis.transactions.append(list(self.queued))
        out = []
        for name, args, kwargs in self.queued:
            out.append(await getattr(self.redis, name)(*args, **kwargs))
        return out


class FakeRedis:
    def __init__(self):
        self.values, self.ttls, self.sets, self.transactions, self.fail = {}, {}, {}, [], None

    def pipeline(self, transaction=True):
        assert transaction is True
        return FakePipeline(self)

    async def mget(self, *keys):
        if self.fail:
            raise self.fail
        return [None if k not in self.values else str(self.values[k]) for k in keys]

    async def ttl(self, key):
        return self.ttls.get(key, -2)

    async def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    async def expire(self, key, seconds, nx=False):
        if nx and key in self.ttls:
            return False
        self.ttls[key] = seconds
        return True

    async def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)
        return 1

    async def scard(self, key):
        return len(self.sets.get(key, ()))

    async def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise self.fail
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True


@pytest.fixture
def env(monkeypatch):
    redis = FakeRedis()
    state = SimpleNamespace(redis=redis, stats=[], spawned=[], logs=[])
    monkeypatch.setattr(login_limits, "get_rate_limit_redis", lambda: redis)

    async def fake_stats(attempt, counts, *, device_reached, ip_reached):
        state.stats.append((counts, device_reached, ip_reached))

    def fake_spawn(coro):
        state.spawned.append(coro.cr_code.co_name)
        coro.close()

    class Log:
        def __getattr__(self, level):
            return lambda event, **kw: state.logs.append((level, event, kw))

    monkeypatch.setattr(login_limits, "_record_stats", fake_stats)
    monkeypatch.setattr(login_limits, "_spawn", fake_spawn)
    monkeypatch.setattr(login_limits, "log", Log())
    login_limits.reset_fail_open_state()
    yield state
    login_limits.reset_fail_open_state()


def attempt(mac="aa:bb:cc:dd:ee:01", ip="203.0.113.7", op="op-1"):
    return Attempt(operator_id=op, ip=ip, mac=login_limits.normalize_mac(mac), site_id=None)


# ── MAC and keys ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, mac", [
    ("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF"), ("AA-BB-CC-DD-EE-FF", "AA:BB:CC:DD:EE:FF"),
    ("aabbccddeeff", "AA:BB:CC:DD:EE:FF"), (" AA:BB:CC:DD:EE:FF ", "AA:BB:CC:DD:EE:FF"),
    ("AA:BB-CC:DD:EE:FF", None), ("AA:BB:CC:DD:EE", None), ("GG:BB:CC:DD:EE:FF", None),
    ("$(mac-esc)", None), ("", None), (None, None),
])
def test_normalize_mac(raw, mac):
    assert login_limits.normalize_mac(raw) == mac


def test_keys():
    a = attempt()
    assert a.device_key == "rl:portal-auth-fail:mac:AA:BB:CC:DD:EE:01"
    assert a.ip_key == "rl:portal-auth-fail:ip:203.0.113.7"
    assert attempt(mac="nonsense").device_key == "rl:portal-auth-fail:nomac:203.0.113.7"


@pytest.mark.parametrize("raw, site", [
    ("5f0c1b8e-9a51-4c43-9d36-2f4a1c7e8b10", "5f0c1b8e-9a51-4c43-9d36-2f4a1c7e8b10"),
    (uuid.UUID("5f0c1b8e-9a51-4c43-9d36-2f4a1c7e8b10"), "5f0c1b8e-9a51-4c43-9d36-2f4a1c7e8b10"),
    ("None", None), ("", None), (None, None), ("not-a-uuid", None),
])
def test_site_or_none(raw, site):
    assert login_limits.site_or_none(raw) == site


# ── check ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_under_the_limits_passes(env):
    env.redis.values.update({attempt().device_key: 4, attempt().ip_key: 29})
    await login_limits.check(attempt(), LIMITS)


@pytest.mark.asyncio
async def test_device_at_its_limit_waits_out_the_window(env):
    a = attempt()
    env.redis.values[a.device_key], env.redis.ttls[a.device_key] = 5, 601
    with pytest.raises(HTTPException) as exc:
        await login_limits.check(a, LIMITS)
    assert exc.value.status_code == 429
    assert exc.value.detail == "Too many wrong codes from this device. Try again in 11 minutes."


@pytest.mark.asyncio
async def test_site_at_its_limit_blocks_every_device(env):
    a = attempt(mac="aa:bb:cc:dd:ee:99")
    env.redis.values[a.ip_key], env.redis.ttls[a.ip_key] = 30, 30
    with pytest.raises(HTTPException) as exc:
        await login_limits.check(a, LIMITS)
    assert exc.value.detail == "Too many wrong codes on this hotspot. Try again in 1 minute."


@pytest.mark.asyncio
async def test_no_mac_shares_one_allowance_per_ip(env):
    env.redis.values[attempt(mac=None).device_key] = 5
    with pytest.raises(HTTPException):
        await login_limits.check(attempt(mac="garbage"), LIMITS)
    await login_limits.check(attempt(mac="aa:bb:cc:dd:ee:02"), LIMITS)  # a real MAC has its own


# ── record_failure ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failure_counts_in_one_transaction_with_fixed_windows(env):
    a = attempt()
    await login_limits.record_failure(a, LIMITS)
    await login_limits.record_failure(a, LIMITS)
    assert len(env.redis.transactions) == 2
    ops = [(name, args) for name, args, _ in env.redis.transactions[0]]
    assert ops[:6] == [("incr", (a.device_key,)), ("expire", (a.device_key, 900)), ("incr", (a.ip_key,)),
                       ("expire", (a.ip_key, 900)), ("incr", (a.operator_key,)), ("expire", (a.operator_key, 900))]
    assert all(kw == {"nx": True} for name, _, kw in env.redis.transactions[0] if name == "expire")
    assert env.stats[-1] == ({"device": 2, "ip": 2, "operator": 2, "operator_ips": 1}, False, False)
    assert env.spawned == []


@pytest.mark.asyncio
async def test_site_blocked_alert_once_when_the_ip_reaches_its_limit(env):
    for i in range(35):
        await login_limits.record_failure(attempt(mac=f"aa:bb:cc:dd:{i:02x}:01"), LIMITS)
    assert env.spawned == ["_send_alert"]
    assert [s[2] for s in env.stats].count(True) == 1  # ip_limit_reached once, on the 30th
    assert env.stats[29][2] is True


@pytest.mark.asyncio
async def test_guessing_alert_once_an_hour_per_operator(env):
    limits = Limits(device=5, ip=30, operator_alert=10)
    for i in range(25):
        await login_limits.record_failure(attempt(mac=f"aa:bb:cc:dd:{i:02x}:02", ip=f"198.51.100.{i % 5}"), limits)
    assert env.spawned == ["_send_alert"]
    assert ("warning", "portal_login_guessing_suspected") in [(lvl, ev) for lvl, ev, _ in env.logs]


# ── Redis down: fail open, log, alert ─────────────────────────────────────

@pytest.mark.asyncio
async def test_redis_down_fails_open_logs_and_alerts_once(env):
    env.redis.fail = RedisConnectionError("Connection refused")
    a = attempt()
    for _ in range(5):
        await login_limits.check(a, LIMITS)          # no 429, no exception
        await login_limits.record_failure(a, LIMITS)  # still recorded in the stats
    errors = [kw for lvl, ev, kw in env.logs if ev == "portal_login_limits_fail_open"]
    assert len(errors) == 1 and errors[0]["error_type"] == "ConnectionError"
    assert env.spawned == ["_send_unavailable_alert"]
    assert len(env.stats) == 5 and all(s == (None, False, False) for s in env.stats)


@pytest.mark.asyncio
async def test_redis_down_alert_repeats_after_an_hour(env, monkeypatch):
    env.redis.fail = RedisConnectionError("down")
    clock = [1000.0]
    monkeypatch.setattr(login_limits.time, "monotonic", lambda: clock[0])
    await login_limits.check(attempt(), LIMITS)
    clock[0] += 3599
    await login_limits.check(attempt(), LIMITS)
    clock[0] += 2
    await login_limits.check(attempt(), LIMITS)
    assert env.spawned == ["_send_unavailable_alert", "_send_unavailable_alert"]


@pytest.mark.asyncio
async def test_a_bug_in_the_limiter_fails_open_too(env, monkeypatch):
    monkeypatch.setattr(login_limits, "get_rate_limit_redis", lambda: (_ for _ in ()).throw(RuntimeError("bug")))
    await login_limits.check(attempt(), LIMITS)
    await login_limits.record_failure(attempt(), LIMITS)
    assert env.spawned == ["_send_unavailable_alert"]


# ── the limits come from platform settings ────────────────────────────────

class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def stored(monkeypatch):
    from src.modules.platform import settings_service

    values = {}

    async def fake_get_setting(db, key):
        if isinstance(values.get(key), Exception):
            raise values[key]
        return values.get(key, settings_service._default(key))

    monkeypatch.setattr(login_limits, "async_session_factory", lambda: _Session())
    monkeypatch.setattr(settings_service, "get_setting", fake_get_setting)
    login_limits.reset_limits_cache()
    yield values
    login_limits.reset_limits_cache()


@pytest.mark.asyncio
async def test_defaults_are_5_30_50(stored):
    assert await login_limits.current_limits() == Limits(device=5, ip=30, operator_alert=50)


@pytest.mark.asyncio
async def test_stored_limits_win_and_bad_ones_fall_back(stored):
    stored.update({"portal_login_fail_limit_mac": "3", "portal_login_fail_limit_ip": "abc",
                   "portal_login_fail_alert_operator": "0"})
    assert await login_limits.current_limits() == Limits(device=3, ip=30, operator_alert=50)


@pytest.mark.asyncio
async def test_limits_are_cached_for_a_minute(stored, monkeypatch):
    clock = [50.0]
    monkeypatch.setattr(login_limits.time, "monotonic", lambda: clock[0])
    stored["portal_login_fail_limit_mac"] = "3"
    assert (await login_limits.current_limits()).device == 3
    stored["portal_login_fail_limit_mac"] = "7"
    clock[0] += 59
    assert (await login_limits.current_limits()).device == 3
    clock[0] += 2
    assert (await login_limits.current_limits()).device == 7


@pytest.mark.asyncio
async def test_settings_lookup_failure_uses_config(stored):
    stored["portal_login_fail_limit_ip"] = RuntimeError("db down")
    assert await login_limits.current_limits() == Limits(device=5, ip=30, operator_alert=50)


# ── the settings endpoint ─────────────────────────────────────────────────

@pytest.fixture
def settings_api(monkeypatch):
    from src.db.base import get_db
    from src.middleware.auth import get_platform_owner_context
    from src.modules.platform import routes as platform_routes
    from src.modules.platform import settings_service

    written = {}

    async def fake_set_setting(db, key, value):
        written[key] = value

    async def fake_get_all_settings(db):
        return dict(written)

    class _Db:
        def add(self, row):
            pass

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


def test_limit_settings_are_registered():
    from src.modules.platform import settings_service

    for key in ("portal_login_fail_limit_mac", "portal_login_fail_limit_ip", "portal_login_fail_alert_operator"):
        assert settings_service.PLATFORM_SETTING_KEYS[key] == key


def test_settings_accept_whole_number_limits(settings_api):
    res = settings_api.client.put("/api/v1/platform/settings", json={
        "portal_login_fail_limit_mac": " 8 ", "portal_login_fail_limit_ip": "040", "portal_login_fail_alert_operator": "10000",
    })
    assert res.status_code == 200, res.text
    assert settings_api.written == {"portal_login_fail_limit_mac": "8", "portal_login_fail_limit_ip": "40",
                                    "portal_login_fail_alert_operator": "10000"}


@pytest.mark.parametrize("raw", ["", "0", "10001", "-1", "2.5", "five", "²"])
def test_settings_refuse_bad_limits_and_write_nothing(settings_api, raw):
    res = settings_api.client.put("/api/v1/platform/settings", json={
        "platform_trial_days": "14", "portal_login_fail_limit_ip": raw,
    })
    assert res.status_code == 400
    assert "between 1 and 10000" in res.json()["detail"]
    assert settings_api.written == {}
