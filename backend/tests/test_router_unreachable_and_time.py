"""A router that is briefly out of reach, and the router clock the wizard sets.

Covers: the RouterOS connect retried once after a timeout (and never after a
refused login or a failed command), the call's time budget leaving room for
that retry, the reconciliation job recording an unreachable router (a failed
connect or a timeout) once it has failed UNREACHABLE_AFTER runs in a row and
again when it comes back (with every failure still recorded when there is no
Redis to count in), and the wizard's final step setting NTP and the time zone
without failing on a time-sync error.

No server and no database: RouterOS, Redis and the sessions are fakes. Safe to
run anywhere, including the production container.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from routeros_api.exceptions import RouterOsApiConnectionError

from src.db.models import RouterProvisionLog
from src.jobs import reconcile_hotspot as rh
from src.modules.mikrotik import api_service, setup_routes, setup_service
from src.modules.mikrotik.api_service import MikroTikAPIService, MikroTikOperationError, RouterAccess, failed_to_connect
from src.modules.mikrotik.setup_types import NatApplyRequest

from test_hotspot_access_revocation import ReconcileDb, ReconcileService, _router, _run_job


# ── connect retry ─────────────────────────────────────────────────────────

class FakeApi:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on

    def get_resource(self, path):
        api = self

        class Resource:
            def call(self, command, arguments=None, queries=None):
                if (path, command) == api.fail_on:
                    raise Exception("failure: no such item")
                return []
        return Resource()


ACCESS = RouterAccess(router_id="r1", host="10.100.0.2", api_port=8728, api_username="admin", api_password="pw",
                      use_ssl=False, nas_secret="")


def _service(monkeypatch, outcomes, api=None):
    """A service whose connect() answers from ``outcomes`` in turn: an exception
    to raise, or anything else for a working connection."""
    svc = MikroTikAPIService.__new__(MikroTikAPIService)
    svc.connection_timeout, svc.command_timeout = 10, 30
    svc.connects, svc.sleeps = [], []
    queue = list(outcomes)

    def connect(router_id=None, **kw):
        svc.connects.append(kw["host"])
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return api_service.MikroTikConnection(pool=None, api=api or FakeApi())

    monkeypatch.setattr(svc, "connect", connect, raising=False)
    monkeypatch.setattr(svc, "disconnect", lambda connection: None, raising=False)
    monkeypatch.setattr(api_service.time, "sleep", lambda seconds: svc.sleeps.append(seconds))
    return svc


def _op(runner, data):
    runner.execute("/system/resource", "print")
    return "ok"


def _timeout():
    return RouterOsApiConnectionError("timed out")


def test_a_timed_out_connect_is_retried_once_and_the_operation_runs(monkeypatch):
    svc = _service(monkeypatch, [_timeout(), "ok"])
    result, commands = svc._execute_connected_operation(ACCESS, _op, {})
    assert result == "ok" and svc.connects == ["10.100.0.2", "10.100.0.2"]
    assert svc.sleeps == [api_service.CONNECT_RETRY_DELAY]
    first, second, command = commands
    assert (first["path"], first["status"], first["error"]) == ("connect", "failed", "Connection timed out")
    assert (second["path"], second["status"], second["attempt"]) == ("connect", "success", 2)
    assert (command["path"], command["status"]) == ("/system/resource", "success")


def test_two_timed_out_connects_fail_as_never_connected(monkeypatch):
    svc = _service(monkeypatch, [_timeout(), _timeout()])
    with pytest.raises(MikroTikOperationError) as caught:
        svc._execute_connected_operation(ACCESS, _op, {})
    assert caught.value.status == "timeout" and len(svc.connects) == 2
    assert [c["status"] for c in caught.value.commands] == ["failed", "failed"]
    assert failed_to_connect(caught.value)


def test_a_refused_login_is_not_retried_and_is_not_unreachable(monkeypatch):
    svc = _service(monkeypatch, [RouterOsApiConnectionError("invalid user name or password (6)")])
    with pytest.raises(MikroTikOperationError) as caught:
        svc._execute_connected_operation(ACCESS, _op, {})
    assert caught.value.status == "auth_failed" and len(svc.connects) == 1 and svc.sleeps == []
    assert not failed_to_connect(caught.value)


def test_a_refused_connection_is_not_retried_but_is_unreachable(monkeypatch):
    svc = _service(monkeypatch, [ConnectionRefusedError("Connection refused")])
    with pytest.raises(MikroTikOperationError) as caught:
        svc._execute_connected_operation(ACCESS, _op, {})
    assert len(svc.connects) == 1 and svc.sleeps == []
    assert failed_to_connect(caught.value)


def test_a_failed_command_is_not_retried_and_is_not_unreachable(monkeypatch):
    svc = _service(monkeypatch, ["ok"], api=FakeApi(fail_on=("/system/resource", "print")))
    with pytest.raises(MikroTikOperationError) as caught:
        svc._execute_connected_operation(ACCESS, _op, {})
    assert len(svc.connects) == 1 and svc.sleeps == []
    assert not failed_to_connect(caught.value)
    assert [c["path"] for c in caught.value.commands] == ["connect", "/system/resource"]


def test_an_operation_that_overran_is_not_unreachable():
    assert not failed_to_connect(MikroTikOperationError("Connection timed out", commands=[], status="timeout"))


def test_the_time_budget_leaves_room_for_the_retry_and_status_is_set_once(monkeypatch):
    svc = MikroTikAPIService.__new__(MikroTikAPIService)
    svc.connection_timeout, svc.command_timeout = 10, 30
    seen, statuses = {}, []

    async def load(router_id):
        return ACCESS

    async def run_sync(func, *args, timeout=None):
        seen["timeout"] = timeout
        return "ok", []

    async def status(router_id, value):
        statuses.append(value)

    monkeypatch.setattr(svc, "_load_router_access", load, raising=False)
    monkeypatch.setattr(svc, "_run_sync", run_sync, raising=False)
    monkeypatch.setattr(svc, "_update_router_connection_status", status, raising=False)
    asyncio.run(svc._run_router_operation("r1", _op))
    assert seen["timeout"] == 30 + 10 + api_service.CONNECT_RETRY_DELAY and statuses == ["online"]
    asyncio.run(svc._run_router_operation("r1", _op, timeout=60))
    assert seen["timeout"] == 60 + 10 + api_service.CONNECT_RETRY_DELAY


# ── reconciliation: unreachable after N runs in a row ─────────────────────

class FakeRedis:
    def __init__(self, broken=False):
        self.values, self.ttls, self.broken = {}, {}, broken

    def _check(self):
        if self.broken:
            raise ConnectionError("redis down")

    async def incr(self, key):
        self._check()
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def expire(self, key, seconds):
        self._check()
        self.ttls[key] = seconds

    async def get(self, key):
        self._check()
        value = self.values.get(key)
        return None if value is None else str(value).encode()

    async def delete(self, key):
        self._check()
        self.values.pop(key, None)


NEVER_CONNECTED = MikroTikOperationError(
    "Connection timed out", status="timeout",
    commands=[{"path": "connect", "status": "failed"}, {"path": "connect", "status": "failed", "attempt": 2}],
)


def _rows(db):
    return [row for row in db.added if isinstance(row, RouterProvisionLog)]


def _runs(monkeypatch, router, redis, outcomes):
    """Run the job once per outcome (an exception, or None for a compliant
    router) and return the provision-log rows each run added."""
    added = []
    for outcome in outcomes:
        svc = ReconcileService(active=[], cookies=[], fail=outcome)
        if outcome is None:
            async def compliant(router_id):
                return []
            svc.enforce_cookie_policy = compliant
        db = ReconcileDb([router], [], [])
        _run_job(monkeypatch, db, svc, ctx={"redis": redis} if redis is not None else None)
        added.append(_rows(db))
    return added


def test_unreachable_is_recorded_once_at_the_threshold_and_again_on_recovery(monkeypatch):
    router, redis = _router(), FakeRedis()
    per_run = _runs(monkeypatch, router, redis, [NEVER_CONNECTED] * 5 + [None])
    assert [len(rows) for rows in per_run] == [0, 0, 1, 0, 0, 1]

    (down,) = per_run[2]
    assert (down.triggered_by, down.status, down.action) == (rh.UNREACHABLE_TRIGGER, "failed", "update_hotspot")
    assert down.commands_executed == [{"event": "unreachable", "failed_runs": 3, "error": "Connection timed out"}]
    assert "3 runs in a row" in down.error_message

    (up,) = per_run[5]
    assert (up.triggered_by, up.status) == (rh.UNREACHABLE_TRIGGER, "success")
    assert up.commands_executed == [{"event": "reachable_again", "failed_runs": 5}]
    assert redis.values == {}
    assert set(redis.ttls.values()) == {rh.UNREACHABLE_STREAK_TTL}


def test_a_short_blip_records_nothing(monkeypatch):
    redis = FakeRedis()
    per_run = _runs(monkeypatch, _router(), redis, [NEVER_CONNECTED, NEVER_CONNECTED, None, NEVER_CONNECTED])
    assert [len(rows) for rows in per_run] == [0, 0, 0, 0]
    assert list(redis.values.values()) == [1]  # the recovery reset the count


def test_streaks_are_counted_per_router(monkeypatch):
    redis = FakeRedis()
    first, second = _router(), _router()
    _runs(monkeypatch, first, redis, [NEVER_CONNECTED] * 2)
    assert [len(rows) for rows in _runs(monkeypatch, second, redis, [NEVER_CONNECTED])] == [0]
    assert sorted(redis.values.values()) == [1, 2]


def test_without_redis_every_failure_is_recorded_as_before(monkeypatch):
    per_run = _runs(monkeypatch, _router(), None, [NEVER_CONNECTED] * 2)
    assert [[(r.triggered_by, r.status) for r in rows] for rows in per_run] == [
        [(rh.COOKIE_POLICY_TRIGGER, "failed")], [(rh.COOKIE_POLICY_TRIGGER, "failed")]]


def test_with_redis_down_every_failure_is_recorded(monkeypatch):
    per_run = _runs(monkeypatch, _router(), FakeRedis(broken=True), [NEVER_CONNECTED] * 2 + [None])
    assert [len(rows) for rows in per_run] == [1, 1, 0]
    assert all(rows[0].triggered_by == rh.COOKIE_POLICY_TRIGGER for rows in per_run[:2])


def test_a_refused_login_is_recorded_every_run_and_ends_a_streak(monkeypatch):
    redis = FakeRedis()
    router = _router()
    _runs(monkeypatch, router, redis, [NEVER_CONNECTED] * 2)
    refused = MikroTikOperationError("Authentication failed", status="auth_failed",
                                     commands=[{"path": "connect", "status": "failed"}])
    per_run = _runs(monkeypatch, router, redis, [refused, refused])
    assert [[(r.triggered_by, r.error_message) for r in rows] for rows in per_run] == [
        [(rh.COOKIE_POLICY_TRIGGER, "Authentication failed")]] * 2
    assert redis.values == {}


def test_a_failed_command_is_recorded_and_ends_a_streak(monkeypatch):
    redis = FakeRedis()
    router = _router()
    _runs(monkeypatch, router, redis, [NEVER_CONNECTED] * 4)
    failed = MikroTikOperationError("failure: no such item", status="offline",
                                    commands=[{"path": "connect", "status": "success"},
                                              {"path": "/ip/hotspot/profile", "status": "failed"}])
    (rows,) = _runs(monkeypatch, router, redis, [failed])
    assert [(r.triggered_by, r.status) for r in rows] == [(rh.COOKIE_POLICY_TRIGGER, "failed")]
    assert redis.values == {}


def test_a_command_that_timed_out_counts_towards_the_streak(monkeypatch):
    # A lossy link: the connect got through, then the first command stalled.
    stalled = MikroTikOperationError("Connection timed out", status="timeout",
                                     commands=[{"path": "connect", "status": "success"},
                                               {"path": "/ip/hotspot/profile", "status": "failed"}])
    redis = FakeRedis()
    per_run = _runs(monkeypatch, _router(), redis, [stalled, NEVER_CONNECTED, stalled])
    assert [len(rows) for rows in per_run] == [0, 0, 1]
    assert per_run[2][0].triggered_by == rh.UNREACHABLE_TRIGGER


def test_an_operation_that_overran_counts_towards_the_streak(monkeypatch):
    overran = MikroTikOperationError("Connection timed out", commands=[], status="timeout")
    per_run = _runs(monkeypatch, _router(), FakeRedis(), [overran] * 3)
    assert [len(rows) for rows in per_run] == [0, 0, 1]


def test_the_failed_run_still_skips_the_router_and_is_counted(monkeypatch):
    svc = ReconcileService(active=[], cookies=[], fail=NEVER_CONNECTED)
    db = ReconcileDb([_router()], [], [])
    stats = _run_job(monkeypatch, db, svc, ctx={"redis": FakeRedis()})
    assert stats["router_errors"] == 1 and stats["routers_checked"] == 0 and db.rollbacks == 1


# ── wizard: router clock ──────────────────────────────────────────────────

class RecordingRunner:
    def __init__(self):
        self.calls = []

    def execute(self, path, command="print", *, params=None, queries=None):
        self.calls.append((path, command, dict(params or {})))
        return []


def test_time_op_sets_the_time_zone_and_the_ntp_client():
    runner = RecordingRunner()
    assert setup_service._op_apply_time(runner, {}) == "time sync configured"
    assert runner.calls == [
        ("/system/clock", "set", {"time-zone-autodetect": "no", "time-zone-name": "Africa/Accra"}),
        ("/system/ntp/client", "set", {"enabled": "yes", "servers": "0.pool.ntp.org,1.pool.ntp.org"}),
    ]


def test_time_commands_are_in_the_nat_terminal_commands():
    lines = setup_service.nat_terminal_commands({"hotspot_network": "192.168.1.0/24", "wan_interface": "ether1"})
    assert lines[-2:] == setup_service.time_terminal_commands()
    assert "time-zone-name=Africa/Accra" in lines[-2] and "servers=0.pool.ntp.org,1.pool.ntp.org" in lines[-1]


class FakeSetup:
    def __init__(self, time_error=None):
        self.time_error, self.calls = time_error, []

    async def apply_nat(self, router_id, data):
        self.calls.append("nat")
        return "NAT & firewall configured", [{"path": "/ip/firewall/nat"}]

    async def apply_portal(self, router_id, base, token):
        self.calls.append("portal")
        return "portal configured", [{"path": "/tool"}]

    async def apply_time(self, router_id):
        self.calls.append("time")
        if self.time_error:
            raise self.time_error
        return "time sync configured", [{"path": "/system/ntp/client"}]


def _apply_nat(monkeypatch, fake_setup, portal_base="http://ip-admin-portal.duckdns.org"):
    router_id = uuid.uuid4()
    captured = {}

    async def load(db, rid, tenant):
        return SimpleNamespace(id=rid, is_online=True, wg_enabled=True, wg_is_connected=True)

    async def run_apply(db, rid, tenant, section, action, operation_coro, *, config_to_store, terminal_commands):
        captured["message"], captured["commands"] = await operation_coro
        captured["terminal"] = terminal_commands
        return captured

    monkeypatch.setattr(setup_routes, "_load_router", load)
    monkeypatch.setattr(setup_routes, "_run_apply", run_apply)
    monkeypatch.setattr(setup_routes, "setup", fake_setup)
    monkeypatch.setattr(setup_routes, "get_settings", lambda: SimpleNamespace(effective_portal_public_base_url=portal_base))
    monkeypatch.setattr(setup_routes, "create_portal_router_token", lambda rid: "token")
    body = NatApplyRequest(wan_interface="ether1", hotspot_network="192.168.1.0/24")
    return asyncio.run(setup_routes.apply_nat(router_id, body, db=None, tenant=SimpleNamespace()))


def test_nat_step_sets_the_clock_after_nat_and_portal(monkeypatch):
    fake = FakeSetup()
    result = _apply_nat(monkeypatch, fake)
    assert fake.calls == ["nat", "portal", "time"]
    assert result["message"] == "NAT & firewall configured; portal configured; time sync configured"
    assert [c["path"] for c in result["commands"]] == ["/ip/firewall/nat", "/tool", "/system/ntp/client"]
    assert result["terminal"][-2:] == setup_service.time_terminal_commands()


def test_nat_step_sets_the_clock_even_without_a_portal_base(monkeypatch):
    fake = FakeSetup()
    result = _apply_nat(monkeypatch, fake, portal_base="")
    assert fake.calls == ["nat", "time"]
    assert result["message"] == "NAT & firewall configured; portal redirect skipped (portal base URL not configured); time sync configured"


def test_a_time_sync_failure_is_reported_without_failing_the_step(monkeypatch):
    error = MikroTikOperationError("failure: unknown parameter", status="offline",
                                   commands=[{"path": "connect", "status": "success"},
                                             {"path": "/system/clock", "status": "failed"}])
    result = _apply_nat(monkeypatch, FakeSetup(time_error=error))
    assert result["message"].endswith("; time sync not configured (failure: unknown parameter)")
    assert [c["path"] for c in result["commands"]] == ["/ip/firewall/nat", "/tool", "connect", "/system/clock"]


class ExpiringRouter:
    """A router row that, like a real one, can't be read once a rollback has
    expired it until it is reloaded (SQLAlchemy raises MissingGreenlet)."""

    def __init__(self, **values):
        object.__setattr__(self, "_values", dict(vars(_router(**values))))
        object.__setattr__(self, "expired", False)

    def __getattr__(self, name):
        if object.__getattribute__(self, "expired"):
            raise RuntimeError(f"MissingGreenlet: read {name} of an expired row")
        return object.__getattribute__(self, "_values")[name]


class ExpiringDb(ReconcileDb):
    async def rollback(self):
        await super().rollback()
        for router in self.routers:
            object.__setattr__(router, "expired", True)

    async def refresh(self, obj):
        object.__setattr__(obj, "expired", False)


def test_a_router_that_fails_without_writing_does_not_stop_the_run(monkeypatch):
    # The first router fails before anything was committed, so the rollback
    # expires both rows; the job must still log it and go on to the second.
    failing, healthy = ExpiringRouter(), ExpiringRouter()
    calls = []

    class Service(ReconcileService):
        async def enforce_cookie_policy(self, router_id):
            calls.append(router_id)
            if router_id == str(object.__getattribute__(failing, "_values")["id"]):
                raise NEVER_CONNECTED
            return []

    db = ExpiringDb([failing, healthy], [], [])
    stats = _run_job(monkeypatch, db, Service(active=[], cookies=[]), ctx={"redis": FakeRedis()})
    assert stats["router_errors"] == 1 and stats["routers_checked"] == 1 and db.rollbacks == 1
    assert len(calls) == 2
