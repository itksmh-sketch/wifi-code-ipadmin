"""Regression tests for the shared rate limiter (src/middleware/rate_limit.py).

1. The counter's INCR and its window EXPIRE must reach Redis as one MULTI/EXEC with
   EXPIRE NX. As two separate commands, a connection drop between them left a key
   with no TTL and locked that IP out of the bucket permanently.
2. Every failure fails open (the limiter guards login, the admin API, the captive
   portal and payment webhooks), but never silently: each is classified and logged,
   throttled per kind. Cancellation is not a failure and must propagate.

Fake client only (no Redis needed).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from redis.exceptions import (
    AuthenticationError,
    AuthorizationError,
    BusyLoadingError,
    ConnectionError as RedisConnectionError,
    DataError,
    ExecAbortError,
    MaxConnectionsError,
    OutOfMemoryError,
    ResponseError,
    TimeoutError as RedisTimeoutError,
)

from src.middleware import rate_limit

REDIS_PASSWORD = "never-log-me"


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.queued = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def incr(self, key):
        self.queued.append(("incr", key))

    def expire(self, key, seconds, nx=False):
        self.queued.append(("expire", key, seconds, nx))

    async def execute(self):
        if self.client.fail is not None:
            raise self.client.fail
        self.client.transactions.append(list(self.queued))
        results = []
        for op in self.queued:
            if op[0] == "incr":
                self.client.store[op[1]] = self.client.store.get(op[1], 0) + 1
                results.append(self.client.store[op[1]])
            else:
                results.append(True)
        return results


class FakeRedis:
    def __init__(self, fail=None):
        self.fail = fail
        self.store = {}
        self.transactions = []
        self.connection_pool = SimpleNamespace(
            connection_kwargs={"host": "redis.test", "port": 6379, "db": 0, "password": REDIS_PASSWORD}
        )

    def pipeline(self, transaction=True):
        assert transaction is True, "rate-limit counter must use a MULTI/EXEC transaction"
        return FakePipeline(self)

    async def incr(self, *args, **kwargs):
        raise AssertionError("INCR must not be sent outside the transaction")

    async def expire(self, *args, **kwargs):
        raise AssertionError("EXPIRE must not be sent outside the transaction")


class LogRecorder:
    def __init__(self, broken=False):
        self.broken = broken
        self.events = []

    def _record(self, level, event, **fields):
        if self.broken:
            raise RuntimeError("logging backend exploded")
        self.events.append({"level": level, "event": event, **fields})

    def warning(self, event, **fields):
        self._record("warning", event, **fields)

    def error(self, event, **fields):
        self._record("error", event, **fields)


def _clear_throttle_state():
    for name in ("_fail_open_last_logged", "_fail_open_suppressed"):
        getattr(rate_limit, name, {}).clear()


@pytest.fixture(autouse=True)
def fresh_throttle_state():
    _clear_throttle_state()
    yield
    _clear_throttle_state()


@pytest.fixture
def log(monkeypatch):
    recorder = LogRecorder()
    monkeypatch.setattr(rate_limit, "log", recorder, raising=False)
    return recorder


def use_client(monkeypatch, client):
    monkeypatch.setattr(rate_limit, "get_rate_limit_redis", lambda: client)
    return client


async def call(ip="203.0.113.9", bucket="admin:login", limit=10, window_seconds=60):
    await rate_limit.enforce_rate_limit(ip, bucket, limit=limit, window_seconds=window_seconds)


# ── Atomic counter ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_incr_and_expire_nx_are_one_transaction(monkeypatch, log):
    client = use_client(monkeypatch, FakeRedis())
    await call()
    key = "rl:admin:login:203.0.113.9"
    assert client.transactions == [[("incr", key), ("expire", key, 60, True)]]
    assert log.events == []


@pytest.mark.asyncio
async def test_limit_is_enforced_per_ip_and_429_is_not_swallowed(monkeypatch, log):
    use_client(monkeypatch, FakeRedis())
    for _ in range(2):
        await call(limit=2)
    with pytest.raises(HTTPException) as exc:
        await call(limit=2)
    assert exc.value.status_code == 429
    await call(ip="203.0.113.10", limit=2)  # a different IP has its own counter
    assert log.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "172.18.0.5"])
async def test_local_ips_skip_redis(monkeypatch, log, ip):
    client = use_client(monkeypatch, FakeRedis())
    await call(ip=ip, limit=0)
    assert client.transactions == []


# ── Fail open, classified and logged ─────────────────────────────────────────

FAILURES = [
    (RedisConnectionError("Error 111 connecting to redis.test:6379. Connection refused."), "redis_unavailable", "warning"),
    (RedisTimeoutError("Timeout reading from socket"), "redis_unavailable", "warning"),
    (BusyLoadingError("Redis is loading the dataset in memory"), "redis_unavailable", "warning"),
    (MaxConnectionsError("Too many connections"), "redis_unavailable", "warning"),
    (AuthenticationError("invalid username-password pair"), "redis_rejected", "error"),
    (AuthorizationError("NOPERM"), "redis_rejected", "error"),
    (OutOfMemoryError("OOM command not allowed"), "redis_rejected", "error"),
    (ExecAbortError("EXECABORT Transaction discarded"), "redis_rejected", "error"),
    (ResponseError("ERR wrong number of arguments for 'expire' command"), "redis_rejected", "error"),
    (DataError("Invalid input"), "redis_rejected", "error"),
    (ValueError("not enough values to unpack"), "unexpected", "error"),
    (TypeError("'>' not supported between instances of 'str' and 'int'"), "unexpected", "error"),
    (KeyError("host"), "unexpected", "error"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc, kind, level", FAILURES, ids=[type(f[0]).__name__ for f in FAILURES])
async def test_every_failure_fails_open_and_is_logged_by_kind(monkeypatch, log, exc, kind, level):
    use_client(monkeypatch, FakeRedis(fail=exc))
    await call()  # must not raise: request is allowed
    assert len(log.events) == 1
    event = log.events[0]
    assert (event["event"], event["kind"], event["level"]) == ("rate_limit_fail_open", kind, level)
    assert event["error_type"] == type(exc).__name__
    # Only a bug in the limiter itself carries a traceback.
    assert event["exc_info"] is (exc if kind == "unexpected" else False)


@pytest.mark.asyncio
async def test_log_line_identifies_the_failure_without_leaking_secrets(monkeypatch, log):
    use_client(monkeypatch, FakeRedis(fail=RedisConnectionError("refused " + "x" * 500)))
    await call(ip="203.0.113.77", bucket="reseller:login", limit=10, window_seconds=60)
    event = log.events[0]
    assert {k: event[k] for k in ("bucket", "client_ip", "limit", "window_seconds", "redis", "suppressed_since_last_log")} == {
        "bucket": "reseller:login",
        "client_ip": "203.0.113.77",
        "limit": 10,
        "window_seconds": 60,
        "redis": "redis.test:6379/0",
        "suppressed_since_last_log": 0,
    }
    assert len(event["error"]) == 200
    assert REDIS_PASSWORD not in repr(log.events)


@pytest.mark.asyncio
async def test_client_construction_failure_fails_open_as_unexpected(monkeypatch, log):
    def broken():
        raise ValueError("Redis URL must specify one of the following schemes")

    monkeypatch.setattr(rate_limit, "get_rate_limit_redis", broken)
    await call()
    assert log.events[0]["kind"] == "unexpected" and log.events[0]["redis"] is None


@pytest.mark.asyncio
async def test_fail_open_logging_is_throttled_per_kind(monkeypatch, log):
    clock = [1000.0]
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    use_client(monkeypatch, FakeRedis(fail=RedisConnectionError("refused")))

    for t in (1000.0, 1001.0, 1002.0):  # an outage: three failures inside one interval
        clock[0] = t
        await call()
    assert len(log.events) == 1

    use_client(monkeypatch, FakeRedis(fail=TypeError("bug")))  # a different kind is not hidden by the outage
    clock[0] = 1003.0
    await call()
    assert [e["kind"] for e in log.events] == ["redis_unavailable", "unexpected"]

    use_client(monkeypatch, FakeRedis(fail=RedisConnectionError("refused")))
    clock[0] = 1000.0 + rate_limit.FAIL_OPEN_LOG_INTERVAL_SECONDS + 1
    await call()
    assert log.events[-1]["kind"] == "redis_unavailable"
    assert log.events[-1]["suppressed_since_last_log"] == 2


@pytest.mark.asyncio
async def test_cancellation_propagates_and_is_not_logged(monkeypatch, log):
    use_client(monkeypatch, FakeRedis(fail=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await call()
    assert log.events == []


@pytest.mark.asyncio
async def test_a_logging_fault_does_not_break_the_request(monkeypatch):
    monkeypatch.setattr(rate_limit, "log", LogRecorder(broken=True))
    use_client(monkeypatch, FakeRedis(fail=RedisConnectionError("refused")))
    await call()  # must not raise
