"""Unit tests for operator-facing router removal (soft-delete).

Covers: the removal sequence and its ordering (disconnect active sessions
while the tunnel is still up, THEN tear down WireGuard), the WG-teardown
failure -> retained peer key -> retry-job pickup path, historical data being
untouched (Session/CoAEvent rows added, never removed), idempotency (already
removed), and the DELETE/GET route wiring (tenant isolation, 409, the
include_removed list filter).

No server and no database: sessions are faked, RouterOS/WireGuard calls are
faked. Safe to run anywhere, including the production container.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from src.db.base import get_db
from src.db.models import Router
from src.jobs.wireguard_status import _retry_incomplete_removals
from src.middleware.auth import get_admin_tenant_context
from src.modules.mikrotik import removal, routes
from src.modules.mikrotik.api_service import MikroTikOperationError, RouterCredentialsMissingError
from src.modules.mikrotik.types import ActiveUserInfo
from src.modules.wireguard.service import WireGuardError


def _router(**overrides) -> Router:
    defaults = dict(
        id=uuid.uuid4(),
        isp_operator_id=uuid.uuid4(),
        site_id=uuid.uuid4(),
        name="Osu",
        nas_identifier="osu-router",
        nas_secret="enc",
        is_active=True,
        wg_peer_public_key="peer-pubkey-1",
        wg_peer_private_key_encrypted="enc-priv",
        wg_tunnel_ip="10.100.0.2",
        wg_enabled=True,
    )
    defaults.update(overrides)
    return Router(**defaults)


class FakeApiService:
    def __init__(self, active_users=None, fail_get=None, fail_disconnect_for=None):
        self.active_users = active_users or []
        self.fail_get = fail_get
        self.fail_disconnect_for = fail_disconnect_for or set()
        self.disconnected = []
        self.calls = []

    async def get_active_hotspot_users(self, router_id):
        self.calls.append(("get_active_hotspot_users", router_id))
        if self.fail_get:
            raise self.fail_get
        return self.active_users

    async def disconnect_hotspot_user(self, router_id, active_id):
        self.calls.append(("disconnect_hotspot_user", router_id, active_id))
        if active_id in self.fail_disconnect_for:
            raise MikroTikOperationError("connection refused", commands=[], status="offline")
        self.disconnected.append(active_id)
        return SimpleNamespace()


class FakeWgService:
    def __init__(self, fail_remove=None):
        self.fail_remove = fail_remove
        self.removed_peers = []
        self.deallocated_for = []
        self.calls = []

    async def remove_peer(self, public_key):
        self.calls.append(("remove_peer", public_key))
        if self.fail_remove:
            raise self.fail_remove
        self.removed_peers.append(public_key)

    async def deallocate_tunnel_ip(self, db, router_id):
        self.calls.append(("deallocate_tunnel_ip", router_id))
        self.deallocated_for.append(router_id)


class FakeResult:
    def __init__(self, scalar=None, scalars_list=None):
        self._scalar = scalar
        self._scalars_list = scalars_list or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(first=lambda: (self._scalars_list or [None])[0], all=lambda: self._scalars_list)


class FakeDb:
    """Dispatches by target table name (found in the compiled SQL) rather than
    call order, since how many queries remove_router issues depends on how
    many active users it's processing."""

    def __init__(self, vouchers=None, sessions=None):
        self.vouchers = vouchers or {}   # username -> voucher-like object
        self.sessions = sessions or {}   # username -> session-like object
        self.added = []
        self.commits = 0

    async def execute(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        if "vouchers" in sql:
            for username, voucher in self.vouchers.items():
                if f"'{username}'" in sql:
                    return FakeResult(scalar=voucher)
            return FakeResult(scalar=None)
        if "sessions" in sql:
            for username, session_row in self.sessions.items():
                if f"'{username}'" in sql:
                    return FakeResult(scalars_list=[session_row])
            return FakeResult(scalars_list=[])
        raise AssertionError(f"unexpected query in remove_router: {sql}")

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


# ── remove_router: idempotency ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_already_removed_router_raises_and_touches_nothing():
    router_row = _router(is_active=False)
    db = FakeDb()
    api, wg = FakeApiService(), FakeWgService()
    with pytest.raises(removal.RouterAlreadyRemovedError):
        await removal.remove_router(db, router_row, router_row.isp_operator_id, api_service=api, wg_service=wg)
    assert api.calls == [] and wg.calls == [] and db.commits == 0


# ── remove_router: the common case ────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_active_users_removes_cleanly():
    router_row = _router()
    db = FakeDb()
    api, wg = FakeApiService(active_users=[]), FakeWgService()

    summary = await removal.remove_router(db, router_row, router_row.isp_operator_id, api_service=api, wg_service=wg)

    assert summary.router_reachable is True
    assert summary.sessions_disconnected == 0 and summary.sessions_failed == 0
    assert summary.wireguard_removed is True and summary.wireguard_message is None
    assert wg.removed_peers == ["peer-pubkey-1"]
    assert wg.deallocated_for == [str(router_row.id)]
    assert router_row.is_active is False
    assert router_row.wg_enabled is False
    assert router_row.wg_peer_public_key is None
    assert router_row.wg_peer_private_key_encrypted is None
    assert router_row.wg_tunnel_ip is None
    # 3 incremental commits: is_active first, then the WG-teardown result, then
    # the removal-outcome summary — see the module docstring for why each is
    # its own commit rather than one at the end (live verification caught the
    # drift this prevents: a real remove_peer() had already happened when an
    # unrelated later failure rolled back one big final commit).
    assert db.commits == 3
    # Durable outcome record (migration 040) — set once, alongside is_active.
    assert router_row.removed_at is not None
    assert router_row.removal_router_reachable is True
    assert router_row.removal_sessions_disconnected == 0
    assert router_row.removal_sessions_failed == 0


@pytest.mark.asyncio
async def test_never_tunneled_router_removes_without_calling_remove_peer():
    router_row = _router(wg_peer_public_key=None, wg_enabled=False, wg_tunnel_ip=None)
    db = FakeDb()
    api, wg = FakeApiService(active_users=[]), FakeWgService()

    summary = await removal.remove_router(db, router_row, router_row.isp_operator_id, api_service=api, wg_service=wg)

    assert summary.wireguard_removed is True
    assert wg.removed_peers == []  # nothing to remove — never called
    assert wg.deallocated_for == [str(router_row.id)]  # still called: idempotent, harmless if nothing was allocated
    assert router_row.is_active is False


# ── remove_router: disconnecting already-online customers ────────────────

@pytest.mark.asyncio
async def test_active_user_is_disconnected_with_matching_voucher_and_session():
    router_row = _router()
    voucher = SimpleNamespace(id=uuid.uuid4())
    session_row = SimpleNamespace(id=uuid.uuid4())
    db = FakeDb(vouchers={"kwame01": voucher}, sessions={"kwame01": session_row})
    active = [ActiveUserInfo(id="*3", user="kwame01", address="10.5.50.12")]
    api, wg = FakeApiService(active_users=active), FakeWgService()

    summary = await removal.remove_router(db, router_row, router_row.isp_operator_id, api_service=api, wg_service=wg)

    assert summary.sessions_disconnected == 1 and summary.sessions_failed == 0
    assert api.disconnected == ["*3"]
    assert len(db.added) == 1
    event = db.added[0]
    assert event.session_id == session_row.id
    assert event.voucher_id == str(voucher.id)
    assert event.router_id == router_row.id
    assert event.event_type == "disconnect" and event.status == "confirmed"


@pytest.mark.asyncio
async def test_active_user_with_no_matching_voucher_still_gets_disconnected():
    """Regression test for a real bug live verification caught: coa_events.voucher_id
    is NOT NULL in the actual schema (confirmed against the live database — the
    FakeDb used everywhere else in this file doesn't enforce real constraints, so
    this specific case needs its own check). The disconnect must still happen —
    it's a real RouterOS-level kick, unrelated to whether a voucher can be found —
    but no CoAEvent audit row can be written for it, so none must be attempted."""
    router_row = _router()
    db = FakeDb()  # no vouchers/sessions configured -> lookups return None/[]
    active = [ActiveUserInfo(id="*7", user="stale-manual-entry")]
    api, wg = FakeApiService(active_users=active), FakeWgService()

    summary = await removal.remove_router(db, router_row, router_row.isp_operator_id, api_service=api, wg_service=wg)

    assert summary.sessions_disconnected == 1
    assert api.disconnected == ["*7"]  # the RouterOS-level kick still happened
    assert db.added == []              # ...but no CoAEvent — would violate voucher_id NOT NULL


@pytest.mark.asyncio
async def test_multiple_active_users_all_processed_independently():
    router_row = _router()
    v1, v2 = SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())
    db = FakeDb(vouchers={"user-a": v1, "user-b": v2})
    active = [ActiveUserInfo(id="*1", user="user-a"), ActiveUserInfo(id="*2", user="user-b")]
    api, wg = FakeApiService(active_users=active), FakeWgService()

    summary = await removal.remove_router(db, router_row, router_row.isp_operator_id, api_service=api, wg_service=wg)

    assert summary.sessions_disconnected == 2
    assert set(api.disconnected) == {"*1", "*2"}
    assert {e.voucher_id for e in db.added} == {str(v1.id), str(v2.id)}


@pytest.mark.asyncio
async def test_disconnect_failure_is_counted_but_does_not_block_removal():
    router_row = _router()
    db = FakeDb(vouchers={"b": SimpleNamespace(id=uuid.uuid4())})  # "a" (the failed one) has none
    active = [ActiveUserInfo(id="*1", user="a"), ActiveUserInfo(id="*2", user="b")]
    api = FakeApiService(active_users=active, fail_disconnect_for={"*1"})
    wg = FakeWgService()

    summary = await removal.remove_router(db, router_row, router_row.isp_operator_id, api_service=api, wg_service=wg)

    assert summary.sessions_disconnected == 1 and summary.sessions_failed == 1
    assert api.disconnected == ["*2"]  # only the one that succeeded
    assert len(db.added) == 1          # no CoAEvent for the failed disconnect
    # A disconnect failure must not stop the tunnel teardown or the removal itself.
    assert summary.wireguard_removed is True
    assert router_row.is_active is False
    assert router_row.removal_sessions_disconnected == 1
    assert router_row.removal_sessions_failed == 1


@pytest.mark.asyncio
async def test_unreachable_router_is_reported_honestly_but_still_removed():
    router_row = _router()
    db = FakeDb()
    api = FakeApiService(fail_get=RouterCredentialsMissingError("no credentials on file"))
    wg = FakeWgService()

    summary = await removal.remove_router(db, router_row, router_row.isp_operator_id, api_service=api, wg_service=wg)

    assert summary.router_reachable is False
    assert summary.sessions_disconnected == 0 and summary.sessions_failed == 0
    # Tunnel teardown and is_active are independent of whether we could reach
    # the router for the active-user check — a router that's off the network
    # entirely still gets its tunnel torn down and is marked removed.
    assert summary.wireguard_removed is True
    assert router_row.is_active is False
    # The durable field the UI keys its "may still be online" warning on —
    # False (not None), so the detail page and fleet-list marker can tell
    # "unreachable at removal" apart from "never removed".
    assert router_row.removal_router_reachable is False
    assert router_row.removed_at is not None


# ── remove_router: ordering ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disconnect_happens_before_wireguard_teardown():
    """The RouterOS API needs a live tunnel to reach the router at all — once
    the peer is removed, there is no route left to send a disconnect over."""
    router_row = _router()
    db = FakeDb()
    order = []

    class OrderedApi(FakeApiService):
        async def get_active_hotspot_users(self, router_id):
            order.append("get_active_users")
            return await super().get_active_hotspot_users(router_id)

        async def disconnect_hotspot_user(self, router_id, active_id):
            order.append("disconnect")
            return await super().disconnect_hotspot_user(router_id, active_id)

    class OrderedWg(FakeWgService):
        async def remove_peer(self, public_key):
            order.append("remove_peer")
            return await super().remove_peer(public_key)

    active = [ActiveUserInfo(id="*1", user="a")]
    await removal.remove_router(
        db, router_row, router_row.isp_operator_id,
        api_service=OrderedApi(active_users=active), wg_service=OrderedWg(),
    )
    assert order == ["get_active_users", "disconnect", "remove_peer"]


# ── remove_router: WireGuard teardown failure -> retained key for retry ──

@pytest.mark.asyncio
async def test_wireguard_failure_retains_peer_key_for_the_retry_job():
    router_row = _router()
    db = FakeDb()
    api = FakeApiService(active_users=[])
    wg = FakeWgService(fail_remove=WireGuardError("wg-manager unreachable: connection refused"))

    summary = await removal.remove_router(db, router_row, router_row.isp_operator_id, api_service=api, wg_service=wg)

    assert summary.wireguard_removed is False
    assert "unreachable" in summary.wireguard_message
    # The router IS still marked removed (is_active=false)...
    assert router_row.is_active is False
    # ...but the peer key is deliberately NOT cleared, and the IP NOT
    # deallocated — that combination is exactly what the retry job looks for.
    assert router_row.wg_peer_public_key == "peer-pubkey-1"
    assert router_row.wg_tunnel_ip is not None
    assert wg.deallocated_for == []
    # 2 commits: is_active first, then the removal-outcome summary. No middle
    # commit here — the WG-teardown block only commits when it actually
    # changes wg_* fields, which it doesn't on failure (that's the point).
    assert db.commits == 2


# ── retry job (jobs/wireguard_status.py) ──────────────────────────────────

class FakeQueryDb:
    """Minimal db for _retry_incomplete_removals: one canned select() result."""

    def __init__(self, routers):
        self._routers = routers
        self.commits = 0

    async def execute(self, statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self._routers))

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_retry_job_completes_a_previously_failed_removal():
    router_row = _router(is_active=False)  # still has wg_peer_public_key from the failed attempt
    db = FakeQueryDb([router_row])
    wg = FakeWgService()

    stats = await _retry_incomplete_removals(db, wg)

    assert stats == {"retried": 1, "completed": 1, "still_failing": 0}
    assert wg.removed_peers == ["peer-pubkey-1"]
    assert router_row.wg_peer_public_key is None
    assert router_row.wg_enabled is False
    assert db.commits == 1


@pytest.mark.asyncio
async def test_retry_job_leaves_still_failing_router_untouched_for_next_run():
    router_row = _router(is_active=False)
    db = FakeQueryDb([router_row])
    wg = FakeWgService(fail_remove=WireGuardError("still down"))

    stats = await _retry_incomplete_removals(db, wg)

    assert stats == {"retried": 1, "completed": 0, "still_failing": 1}
    assert router_row.wg_peer_public_key == "peer-pubkey-1"  # untouched, will retry again next run


@pytest.mark.asyncio
async def test_retry_job_query_only_targets_removed_routers_with_a_leftover_peer():
    # This documents the filter's intent; the actual WHERE clause is exercised
    # for real in the live-verification pass (an active router with a peer,
    # and an already-fully-removed router, must both be ignored).
    import inspect
    source = inspect.getsource(_retry_incomplete_removals)
    assert "Router.is_active.is_(False)" in source
    assert "Router.wg_peer_public_key.isnot(None)" in source


# ── HTTP layer: tenant isolation, idempotency, list filtering ────────────

@pytest.fixture
def api_app(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    tenant = SimpleNamespace(isp_operator_id=uuid.uuid4())
    app.dependency_overrides[get_admin_tenant_context] = lambda: tenant
    return SimpleNamespace(app=app, client=TestClient(app), tenant=tenant)


def test_delete_endpoint_maps_already_removed_to_409(api_app, monkeypatch):
    router_row = _router(is_active=False)

    async def fake_db():
        yield SimpleNamespace()

    async def fake_ensure(db, router_id, isp_operator_id):
        return router_row

    async def fake_remove(*args, **kwargs):
        raise removal.RouterAlreadyRemovedError("already removed")

    api_app.app.dependency_overrides[get_db] = fake_db
    monkeypatch.setattr(routes, "_ensure_router_in_tenant", fake_ensure)
    monkeypatch.setattr(routes, "remove_router", fake_remove)

    res = api_app.client.delete(f"/api/v1/admin/routers/{router_row.id}")
    assert res.status_code == 409


def test_delete_endpoint_404s_outside_the_caller_tenant(api_app, monkeypatch):
    async def fake_db():
        yield SimpleNamespace()

    async def fake_ensure(db, router_id, isp_operator_id):
        raise HTTPException(status_code=404, detail="Router not found")

    api_app.app.dependency_overrides[get_db] = fake_db
    monkeypatch.setattr(routes, "_ensure_router_in_tenant", fake_ensure)

    res = api_app.client.delete(f"/api/v1/admin/routers/{uuid.uuid4()}")
    assert res.status_code == 404


def test_list_defaults_to_active_only_and_include_removed_returns_both(api_app):
    active = _router(name="Osu")
    removed = _router(name="Aflao-net", is_active=False)
    site = SimpleNamespace(name="Accra")
    both_rows = [(active, site, None), (removed, site, None)]

    class ListDb:
        def __init__(self):
            self.queries = []

        async def execute(self, statement):
            sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
            self.queries.append(sql)
            # The default (active-only) call adds a router-level is_active filter
            # on top of the tenant scoping; include_removed=true omits it.
            filtered = "routers.is_active IS true" in sql
            return SimpleNamespace(all=lambda: [both_rows[0]] if filtered else both_rows)

    db = ListDb()

    async def fake_db():
        yield db

    api_app.app.dependency_overrides[get_db] = fake_db

    default_res = api_app.client.get("/api/v1/admin/routers")
    assert [r["name"] for r in default_res.json()] == ["Osu"]

    all_res = api_app.client.get("/api/v1/admin/routers?include_removed=true")
    assert sorted(r["name"] for r in all_res.json()) == ["Aflao-net", "Osu"]
    # Confirm the two calls actually issued different SQL (not just coincidence).
    assert db.queries[0] != db.queries[1]
