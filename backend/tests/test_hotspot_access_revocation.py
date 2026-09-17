"""Unit tests for hotspot access revocation and router/DB reconciliation.

Covers: RouterOS cookie clearing (by username and by MAC) inside the manual /
backstop disconnect, the cookie-lifetime policy push, voucher-level revocation
that works without an open session row, the CoA helper clearing cookies even
when CoA fails, the manual-disconnect audit event reflecting the real outcome,
and the reconciliation job's classification — including the original bug state
(valid voucher online with no sessions row) and a deliberately-uncleaned cookie
— on reachable and unreachable routers.

No server and no database: sessions are faked, RouterOS calls are faked. Safe to
run anywhere, including the production container.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.db.base import get_db
from src.jobs import reconcile_hotspot as rh
from src.middleware.auth import get_admin_tenant_context
from src.modules.mikrotik import access_revocation, routes
from src.modules.mikrotik.api_service import HOTSPOT_COOKIE_LIFETIME, MikroTikAPIService, MikroTikOperationError
from src.modules.mikrotik.types import ActiveUserInfo
from src.radius import coa_events

NOW = datetime(2026, 9, 16, 16, 0, tzinfo=timezone.utc)


# ── RouterOS runner fake ──────────────────────────────────────────────────

class FakeRunner:
    def __init__(self, tables):
        self.tables = {k: [dict(r) for r in v] for k, v in tables.items()}
        self.calls = []

    def execute(self, path, command="print", *, params=None, queries=None):
        self.calls.append((path, command, dict(params or {})))
        rows = self.tables.setdefault(path, [])
        if command == "print":
            return [dict(r) for r in rows]
        if command == "remove":
            self.tables[path] = [r for r in rows if r["id"] != params[".id"]]
            return []
        if command == "set":
            for r in rows:
                if r["id"] == params[".id"]:
                    r.update({k: v for k, v in params.items() if k != ".id"})
            return []
        raise AssertionError(command)

    def removed(self, path):
        return [c[2][".id"] for c in self.calls if c[0] == path and c[1] == "remove"]


def _svc():
    return MikroTikAPIService.__new__(MikroTikAPIService)


COOKIES = [
    {"id": "*1", "user": "GNPD", "mac-address": "A4:1F:72:63:11:54"},
    {"id": "*2", "user": "OTHER", "mac-address": "00:11:22:33:44:55"},
    {"id": "*3", "user": "SOMEONE", "mac-address": "a4:1f:72:63:11:54"},  # same device, other voucher
]


def test_disconnect_removes_active_entry_and_its_cookies_by_user_and_mac():
    runner = FakeRunner({
        "/ip/hotspot/active": [{"id": "*A", "user": "GNPD", "mac-address": "A4:1F:72:63:11:54"}],
        "/ip/hotspot/cookie": COOKIES,
    })
    msg = _svc()._sync_disconnect_hotspot_user(runner, {"active_id": "*A"})
    assert runner.removed("/ip/hotspot/active") == ["*A"]
    # MAC match is case-insensitive; the unrelated device's cookie survives.
    assert sorted(runner.removed("/ip/hotspot/cookie")) == ["*1", "*3"]
    assert "2 cookie(s) cleared" in msg


def test_disconnect_of_vanished_entry_still_issues_remove_but_clears_nothing():
    runner = FakeRunner({"/ip/hotspot/active": [], "/ip/hotspot/cookie": COOKIES})
    _svc()._sync_disconnect_hotspot_user(runner, {"active_id": "*gone"})
    assert runner.removed("/ip/hotspot/active") == ["*gone"]
    assert runner.removed("/ip/hotspot/cookie") == []


def test_clear_cookies_with_no_identifiers_is_a_noop():
    runner = FakeRunner({"/ip/hotspot/cookie": COOKIES})
    assert _svc()._sync_clear_hotspot_cookies(runner, {"users": [None, ""], "mac": None}) == 0
    assert runner.calls == []


def test_remove_user_access_matches_any_login_name():
    runner = FakeRunner({
        "/ip/hotspot/active": [
            {"id": "*A", "user": "CODE-1", "mac-address": "AA:AA:AA:AA:AA:AA"},
            {"id": "*B", "user": "KEEP", "mac-address": "BB:BB:BB:BB:BB:BB"},
        ],
        "/ip/hotspot/cookie": [{"id": "*1", "user": "USER-1", "mac-address": "CC:CC:CC:CC:CC:CC"}],
    })
    result = _svc()._sync_remove_hotspot_user_access(runner, {"users": ["USER-1", "CODE-1"]})
    assert result == {"active_removed": 1, "cookies_cleared": 1}
    assert runner.removed("/ip/hotspot/active") == ["*A"]


def test_cookie_policy_push_is_idempotent():
    runner = FakeRunner({
        "/ip/hotspot/profile": [
            {"id": "*0", "name": "default", "http-cookie-lifetime": "3d"},
            {"id": "*1", "name": "hsprof1", "http-cookie-lifetime": "3d"},
        ],
        "/ip/hotspot/user/profile": [{"id": "*0", "name": "default", "mac-cookie-timeout": "3d"}],
    })
    svc = _svc()
    changed = svc._sync_enforce_cookie_policy(runner, {})
    assert len(changed) == 2
    assert runner.tables["/ip/hotspot/profile"][1]["http-cookie-lifetime"] == HOTSPOT_COOKIE_LIFETIME
    # The built-in "default" server profile is never touched.
    assert runner.tables["/ip/hotspot/profile"][0]["http-cookie-lifetime"] == "3d"
    assert svc._sync_enforce_cookie_policy(runner, {}) == []


# ── voucher-level revocation / CoA helper ─────────────────────────────────

def _router(**kw):
    base = dict(id=uuid.uuid4(), name="Win Routerboard", isp_operator_id=uuid.uuid4(), is_active=True, is_online=False,
                wg_enabled=True, wg_is_connected=True, wg_tunnel_ip="10.100.0.5", ip_address=None, nas_secret="enc")
    base.update(kw)
    return SimpleNamespace(**base)


class ScalarDb:
    """Answers the router lookup, then one credential lookup per router."""

    def __init__(self, routers, creds_for):
        self.routers, self.creds_for = routers, creds_for
        self.queries = 0

    async def execute(self, statement):
        self.queries += 1
        sql = str(statement)
        if "router_credentials" in sql:
            rid = statement.compile().params["router_id_1"]
            return SimpleNamespace(scalar_one_or_none=lambda: 1 if rid in self.creds_for else None)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.routers))

    async def flush(self):
        pass


class RecordingService:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def remove_hotspot_user_access(self, router_id, usernames, mac=None):
        self.calls.append(("remove", router_id, list(usernames)))
        if self.fail:
            raise MikroTikOperationError("timed out")
        return {"active_removed": 1, "cookies_cleared": 1}

    async def clear_hotspot_cookies(self, router_id, usernames, mac=None):
        self.calls.append(("clear", router_id, list(usernames), mac))
        return 1


@pytest.mark.asyncio
async def test_revoke_voucher_access_needs_no_session_row_and_skips_unusable_routers():
    live = _router()
    offline = _router(wg_is_connected=False)
    no_creds = _router()
    db = ScalarDb([live, offline, no_creds], creds_for={live.id, offline.id})
    voucher = SimpleNamespace(id=uuid.uuid4(), isp_operator_id=live.isp_operator_id, code="X9RW", username="GNPD")
    svc = RecordingService()

    stats = await access_revocation.revoke_voucher_access(db, voucher, service=svc)

    assert svc.calls == [("remove", str(live.id), ["GNPD", "X9RW"])]
    assert stats == {"routers": 1, "active_removed": 1, "cookies_cleared": 1, "errors": 0}


@pytest.mark.asyncio
async def test_revoke_voucher_access_swallows_router_errors():
    live = _router()
    db = ScalarDb([live], creds_for={live.id})
    voucher = SimpleNamespace(id=uuid.uuid4(), isp_operator_id=live.isp_operator_id, code="X9RW", username=None)
    stats = await access_revocation.revoke_voucher_access(db, voucher, service=RecordingService(fail=True))
    assert stats["errors"] == 1


@pytest.mark.asyncio
async def test_coa_helper_clears_cookies_even_when_coa_fails(monkeypatch):
    router = _router()
    session = SimpleNamespace(username="GNPD", ip_address="192.168.20.13", mac_address="a4:1f:72:63:11:54")
    voucher = SimpleNamespace(code="X9RW", username="GNPD")
    event = SimpleNamespace(status="pending", attempt_count=0, error_message=None, last_attempted_at=None)
    cleared = []

    monkeypatch.setattr(coa_events, "decrypt_secret", lambda s: "secret")
    monkeypatch.setattr(coa_events, "send_disconnect_request", lambda **kw: {"status": "failed", "message": "timeout"})

    async def fake_clear(db, r, names, mac=None):
        cleared.append((r.id, names, mac))
        return 1

    monkeypatch.setattr(coa_events, "clear_router_cookies", fake_clear)

    result = await coa_events.send_disconnect_with_event(ScalarDb([], set()), event=event, router=router, voucher=voucher, session=session)

    assert event.status == "failed"  # cookie clearing never masks a failed CoA
    assert cleared == [(router.id, ["GNPD", "X9RW"], "a4:1f:72:63:11:54")]
    assert result["cookies_cleared"] == 1


# ── manual disconnect route: audit event reflects the real outcome ────────

class RouteDb:
    def __init__(self):
        self.added, self.commits = [], 0

    async def execute(self, statement):
        empty = SimpleNamespace(first=lambda: None, all=lambda: [])
        return SimpleNamespace(scalar_one_or_none=lambda: None, scalars=lambda: empty)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


@pytest.fixture
def disconnect_app(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[get_admin_tenant_context] = lambda: SimpleNamespace(isp_operator_id=uuid.uuid4())
    db = RouteDb()

    async def fake_db():
        yield db

    async def fake_ensure(*args):
        return None

    app.dependency_overrides[get_db] = fake_db
    monkeypatch.setattr(routes, "_ensure_router_in_tenant", fake_ensure)
    return SimpleNamespace(client=TestClient(app, raise_server_exceptions=False), db=db)


def _post_disconnect(app_ns):
    return app_ns.client.post(
        f"/api/v1/admin/routers/{uuid.uuid4()}/disconnect-user",
        json={"active_id": "*A", "voucher_id": str(uuid.uuid4())},
    )


def test_manual_disconnect_success_records_confirmed(disconnect_app, monkeypatch):
    async def ok(router_id, active_id):
        return SimpleNamespace(model_dump=lambda: {"success": True})

    monkeypatch.setattr(routes.service, "disconnect_hotspot_user", ok)
    assert _post_disconnect(disconnect_app).status_code == 200
    (event,) = disconnect_app.db.added
    assert event.status == "confirmed" and event.attempt_count == 1


def test_manual_disconnect_failure_records_failed_not_pending(disconnect_app, monkeypatch):
    async def boom(router_id, active_id):
        raise MikroTikOperationError("Connection timed out")

    monkeypatch.setattr(routes.service, "disconnect_hotspot_user", boom)
    assert _post_disconnect(disconnect_app).status_code >= 400
    (event,) = disconnect_app.db.added
    assert event.status == "failed"
    assert event.attempt_count >= 3  # coa_retry won't fire unrequested CoAs
    assert "timed out" in event.error_message


# ── reconciliation: classification ────────────────────────────────────────

def _voucher(status="active", expires_at=None):
    return SimpleNamespace(id=uuid.uuid4(), status=status, expires_at=expires_at)


def _session(username, started_minutes_ago=30):
    return SimpleNamespace(id=uuid.uuid4(), username=username, started_at=NOW - timedelta(minutes=started_minutes_ago))


def _active(user, uptime="10m"):
    return {"id": "*" + user, "user": user, "address": "192.168.20.13", "mac_address": "A4:1F:72:63:11:54", "uptime": uptime}


def test_original_bug_state_is_flagged_as_untracked():
    # Valid voucher online on the router, but its reconnect never got a sessions
    # row (the 80000001 collision): exactly the state of 2026-09-16 15:5x.
    plan = rh.plan_reconciliation(
        active=[_active("GNPD", uptime="12m4s")],
        cookies=[{"user": "GNPD"}],
        vouchers_by_name={"GNPD": _voucher("active")},
        open_sessions=[],
        now=NOW,
    )
    assert [e["user"] for e in plan.untracked_online] == ["GNPD"]
    assert plan.revoked_online == [] and plan.revoked_cookie == []


def test_untracked_within_grace_period_is_not_flagged():
    plan = rh.plan_reconciliation(
        active=[_active("GNPD", uptime="40s")], cookies=[],
        vouchers_by_name={"GNPD": _voucher()}, open_sessions=[], now=NOW,
    )
    assert plan.untracked_online == []


def test_tracked_valid_client_is_left_alone():
    plan = rh.plan_reconciliation(
        active=[_active("GNPD")], cookies=[{"user": "GNPD"}],
        vouchers_by_name={"GNPD": _voucher()}, open_sessions=[_session("GNPD")], now=NOW,
    )
    assert plan == rh.ReconcilePlan()


@pytest.mark.parametrize("voucher", [
    _voucher("disabled"), _voucher("exhausted"), _voucher("expired"),
    _voucher("active", expires_at=NOW - timedelta(seconds=1)),
])
def test_invalid_voucher_online_and_uncleaned_cookie_are_revoked(voucher):
    plan = rh.plan_reconciliation(
        active=[_active("GNPD")], cookies=[{"user": "GNPD"}],
        vouchers_by_name={"GNPD": voucher}, open_sessions=[_session("GNPD")], now=NOW,
    )
    assert [e["user"] for e in plan.revoked_online] == ["GNPD"]
    assert [c["user"] for c in plan.revoked_cookie] == ["GNPD"]


def test_deliberately_uncleaned_cookie_alone_is_revoked():
    # Client already gone (disconnect succeeded) but the cookie was left behind.
    plan = rh.plan_reconciliation(
        active=[], cookies=[{"user": "GNPD"}],
        vouchers_by_name={"GNPD": _voucher("disabled")}, open_sessions=[], now=NOW,
    )
    assert [c["user"] for c in plan.revoked_cookie] == ["GNPD"]


def test_unknown_names_are_flagged_never_revoked():
    plan = rh.plan_reconciliation(
        active=[_active("local-admin")], cookies=[{"user": "local-admin"}],
        vouchers_by_name={}, open_sessions=[], now=NOW,
    )
    assert [e["user"] for e in plan.unknown_online] == ["local-admin"]
    assert plan.revoked_online == [] and plan.revoked_cookie == []


def test_phantom_session_respects_grace_period():
    old, fresh = _session("GONE", 30), _session("JUSTSTOPPED", 1)
    plan = rh.plan_reconciliation(active=[], cookies=[], vouchers_by_name={}, open_sessions=[old, fresh], now=NOW)
    assert plan.phantom_session == [old]


@pytest.mark.parametrize("value,seconds", [
    ("45s", 45), ("12m4s", 724), ("1w2d3h4m5s", 604800 + 2 * 86400 + 3 * 3600 + 245),
    ("01:02:03", 3723), ("", None), (None, None), ("garbage", None),
])
def test_parse_uptime(value, seconds):
    parsed = rh.parse_uptime(value)
    assert (parsed.total_seconds() if parsed else None) == seconds


# ── reconciliation: reachable vs unreachable routers ──────────────────────

class ReconcileService:
    def __init__(self, active, cookies, fail=None):
        self.active, self.cookies, self.fail = active, cookies, fail
        self.calls = []

    async def enforce_cookie_policy(self, router_id):
        self.calls.append(("policy", router_id))
        if self.fail:
            raise self.fail
        return ["hsprof1 http-cookie-lifetime 3d -> 1m"]

    async def get_active_hotspot_users(self, router_id):
        return [ActiveUserInfo(**a) for a in self.active]

    async def get_hotspot_cookies(self, router_id):
        return self.cookies

    async def remove_hotspot_user_access(self, router_id, usernames, mac=None):
        self.calls.append(("remove", router_id, list(usernames)))
        return {"active_removed": 1, "cookies_cleared": 1}

    async def clear_hotspot_cookies(self, router_id, usernames, mac=None):
        self.calls.append(("clear", router_id, list(usernames)))
        return len(usernames)


class ReconcileDb:
    def __init__(self, routers, vouchers, sessions):
        self.routers, self.vouchers, self.sessions = routers, vouchers, sessions
        self.updates, self.commits, self.rollbacks = [], 0, 0
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, statement):
        sql = str(statement)
        if sql.startswith("UPDATE"):
            self.updates.append(statement)
            return SimpleNamespace()
        if "FROM routers" in sql:
            rows = self.routers
        elif "FROM vouchers" in sql:
            rows = self.vouchers
        else:
            rows = self.sessions
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _run_job(monkeypatch, db, svc, **kw):
    monkeypatch.setattr(rh, "async_session_factory", lambda: db)
    monkeypatch.setattr(rh, "MikroTikAPIService", lambda: svc)
    import asyncio
    return asyncio.run(rh.reconcile_hotspot(**kw))


def test_job_on_reachable_router_pushes_policy_and_revokes_uncleaned_cookie(monkeypatch):
    router = _router()
    disabled = SimpleNamespace(id=uuid.uuid4(), code="X9RW", username="GNPD", status="disabled", expires_at=None)
    svc = ReconcileService(active=[], cookies=[{"id": "*1", "user": "GNPD", "mac_address": "A4:1F:72:63:11:54"}])
    db = ReconcileDb([router], [disabled], [])

    stats = _run_job(monkeypatch, db, svc)

    assert ("policy", str(router.id)) in svc.calls
    assert ("clear", str(router.id), ["GNPD"]) in svc.calls
    # The live config push is recorded durably, not just logged.
    (log_row,) = db.added
    assert (log_row.action, log_row.status, log_row.triggered_by) == ("update_hotspot", "success", rh.COOKIE_POLICY_TRIGGER)
    assert log_row.commands_executed == [{"change": "hsprof1 http-cookie-lifetime 3d -> 1m"}]
    assert stats["routers_checked"] == 1 and stats["cookie_policy_applied"] == 1 and stats["revoked_cookie"] == 1


def test_job_flags_original_bug_state_without_kicking(monkeypatch):
    router = _router()
    valid = SimpleNamespace(id=uuid.uuid4(), code="X9RW", username="GNPD", status="active", expires_at=None)
    svc = ReconcileService(active=[_active("GNPD", "12m")], cookies=[])
    stats = _run_job(monkeypatch, ReconcileDb([router], [valid], []), svc)
    assert stats["untracked_online"] == 1
    assert not any(c[0] == "remove" for c in svc.calls)


def test_job_dry_run_changes_nothing(monkeypatch):
    router = _router()
    disabled = SimpleNamespace(id=uuid.uuid4(), code="X9RW", username="GNPD", status="disabled", expires_at=None)
    svc = ReconcileService(active=[_active("GNPD")], cookies=[{"id": "*1", "user": "GNPD"}])
    db = ReconcileDb([router], [disabled], [_session("STALE")])
    stats = _run_job(monkeypatch, db, svc, dry_run=True)
    assert svc.calls == [] and db.updates == [] and db.commits == 0
    assert stats["revoked_online"] == 1 and stats["revoked_cookie"] == 1 and stats["phantom_session"] == 1


def test_job_skips_unreachable_router_without_touching_it(monkeypatch):
    router = _router(is_online=False, wg_is_connected=False)
    svc = ReconcileService(active=[], cookies=[])
    stats = _run_job(monkeypatch, ReconcileDb([router], [], [_session("STALE")]), svc)
    assert svc.calls == []
    assert stats["routers_skipped"] == 1 and stats["routers_checked"] == 0 and stats["phantom_session"] == 0


def test_job_router_that_times_out_is_counted_and_rolled_back(monkeypatch):
    router = _router()  # DB says reachable, but the API call fails
    svc = ReconcileService(active=[], cookies=[], fail=MikroTikOperationError("Connection timed out"))
    db = ReconcileDb([router], [], [])
    stats = _run_job(monkeypatch, db, svc)
    assert stats["router_errors"] == 1 and db.rollbacks == 1
    # The failed push attempt is itself recorded.
    (log_row,) = db.added
    assert log_row.status == "failed" and "timed out" in log_row.error_message


def test_job_on_compliant_router_records_nothing(monkeypatch):
    router = _router()
    svc = ReconcileService(active=[], cookies=[])

    async def compliant(router_id):
        svc.calls.append(("policy", router_id))
        return []

    svc.enforce_cookie_policy = compliant
    db = ReconcileDb([router], [], [])
    stats = _run_job(monkeypatch, db, svc)
    assert db.added == [] and stats["cookie_policy_applied"] == 0


# ── ordering: CoA first, router-side cleanup after ────────────────────────

class OrderDb:
    def __init__(self, session_row, router_row):
        self.session_row, self.router_row = session_row, router_row
        self.commits = 0

    async def execute(self, statement):
        sql = str(statement)
        row = self.router_row if "FROM routers" in sql else self.session_row
        return SimpleNamespace(scalar_one_or_none=lambda: row)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def _patch_order(monkeypatch, module, calls):
    async def fake_transition(db, voucher_id, status, isp_operator_id):
        return SimpleNamespace(id=voucher_id, isp_operator_id=isp_operator_id, code="C", username="U")

    async def fake_event(db, **kw):
        calls.append("event")
        return SimpleNamespace(status="sent", attempt_count=1)

    async def fake_send(db, **kw):
        calls.append("coa")
        return {}

    async def fake_revoke(db, voucher):
        calls.append("revoke")
        return {}

    monkeypatch.setattr(module, "transition_voucher_status", fake_transition)
    monkeypatch.setattr(module, "create_pending_disconnect_event", fake_event)
    monkeypatch.setattr(module, "send_disconnect_with_event", fake_send)
    monkeypatch.setattr(module, "revoke_voucher_access", fake_revoke)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_session", [True, False])
async def test_disable_sends_coa_before_router_cleanup(monkeypatch, has_session):
    from src.modules.vouchers import engine

    calls = []
    _patch_order(monkeypatch, engine, calls)
    session_row = SimpleNamespace(id=uuid.uuid4(), router_id=uuid.uuid4()) if has_session else None
    db = OrderDb(session_row, _router())

    await engine.disable_voucher_with_disconnect(db, uuid.uuid4(), uuid.uuid4())

    # Untracked client (no session row) still gets the router-side cleanup.
    assert calls == (["event", "coa", "revoke"] if has_session else ["revoke"])


@pytest.mark.asyncio
@pytest.mark.parametrize("has_session", [True, False])
async def test_expiry_sends_coa_before_router_cleanup(monkeypatch, has_session):
    from src.jobs import voucher_expiry

    calls = []
    _patch_order(monkeypatch, voucher_expiry, calls)
    session_row = SimpleNamespace(id=uuid.uuid4(), router_id=uuid.uuid4()) if has_session else None
    voucher = SimpleNamespace(id=uuid.uuid4(), isp_operator_id=uuid.uuid4(), code="C", username="U")
    db = OrderDb(session_row, _router())
    if has_session:
        # _disconnect_open_session re-reads the voucher after the router lookup.
        rows = iter([session_row, _router(), voucher])
        db.execute = lambda statement: _async_result(next(rows))

    await voucher_expiry._transition_and_disconnect(db, voucher, "expired")

    assert calls == (["event", "coa", "revoke"] if has_session else ["revoke"])


async def _async_result(row):
    return SimpleNamespace(scalar_one_or_none=lambda: row)
