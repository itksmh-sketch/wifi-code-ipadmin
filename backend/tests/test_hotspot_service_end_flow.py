"""The end of a cancelled operator's hotspot wind-down, against a REAL database:
jobs.hotspot_service_end (which sessions it disconnects, the once-only mark,
the system audit row), the platform's "End hotspot service now" route,
restore clearing the mark, migration 059's CHECK, and reconcile_hotspot
leaving a wind-down operator's clients alone until the end has passed.

Only the router-facing calls are faked (the CoA socket, the RouterOS API); the
rows, queries, locks and audit writes are real. Runs the FastAPI app
in-process. Only runs against a disposable database at migration 059 or later:

    HOTSPOT_SERVICE_END_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("HOTSPOT_SERVICE_END_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs HOTSPOT_SERVICE_END_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import select, update
    from sqlalchemy.exc import IntegrityError

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import (
        AuditLog, CoAEvent, ISPOperator, Plan, PlatformOwner, Router, RouterCredential, Session, Site, Town, Voucher,
    )
    from src.jobs import hotspot_service_end as hse
    from src.jobs import reconcile_hotspot as rh
    from src.modules.auth.tokens import platform_owner_token_data
    from src.modules.mikrotik.types import ActiveUserInfo
    from src.modules.notifications import dispatcher
    from src.radius import coa_events
    from src.utils.auth import create_platform_owner_access_token, hash_password
    from src.utils.encryption import encrypt_secret

PASSWORD = "Str0ngPassword"
REASON = "Router stolen, end it now XYZZY"


def _now():
    return datetime.now(timezone.utc)


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    coa_calls: list[dict] = []
    coa_result = {"status": "success"}

    def fake_coa(**kwargs):
        coa_calls.append(kwargs)
        hook = env_state.get("during_coa")
        if hook:
            hook()
        return dict(coa_result)

    revoked: list[uuid.UUID] = []

    async def fake_revoke(db, voucher, **kwargs):
        revoked.append(voucher.id)
        return {"routers": 1, "active_removed": 1, "cookies_cleared": 2, "errors": 0}

    async def no_cookies(*args, **kwargs):
        return None

    async def no_notice(**kwargs):
        return None

    env_state: dict = {}
    monkeypatch.setattr(coa_events, "send_disconnect_request", fake_coa)
    monkeypatch.setattr(coa_events, "clear_router_cookies", no_cookies)
    monkeypatch.setattr(hse, "revoke_voucher_access", fake_revoke)
    monkeypatch.setattr(dispatcher, "notify_operator_status_changed", no_notice)

    async with async_session_factory() as db:
        owner = PlatformOwner(
            email=f"owner-{uuid.uuid4().hex[:8]}@throwaway.test", password_hash=hash_password(PASSWORD),
            name="Owner", is_active=True, must_complete_security_setup=False,
        )
        db.add(owner)
        await db.commit()
    token = create_platform_owner_access_token(platform_owner_token_data(owner))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as client:
        env_state.update(client=client, headers={"Authorization": f"Bearer {token}"}, coa_calls=coa_calls,
                         coa_result=coa_result, revoked=revoked)
        yield env_state


# ── fixtures ──────────────────────────────────────────────────────────────

def _state(status: str, ends_in: timedelta | None) -> dict:
    """Column values that satisfy 058's CHECKs. ends_in: wind-down end relative
    to now (cancelled only)."""
    if status == "cancelled":
        now = _now()
        return {"status": "cancelled", "billing_status": "cancelled", "cancelled_at": now - timedelta(days=1),
                "hotspot_service_ends_at": now + ends_in}
    return {"status": status, "billing_status": "active"}


async def make_operator(status="approved", ends_in: timedelta | None = None) -> "ISPOperator":
    slug = f"hse-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Hse {slug}", slug=slug, contact_email=f"{slug}@throwaway.test",
                               monthly_fee_ghs=Decimal("50.00"), **_state(status, ends_in))
        db.add(operator)
        await db.commit()
        return operator


_TUNNEL = iter(range(10, 250))


async def make_router(operator, *, reachable=False, credentials=False) -> "Router":
    async with async_session_factory() as db:
        town = Town(isp_operator_id=operator.id, name=f"T {uuid.uuid4().hex[:6]}", region="Volta")
        db.add(town)
        await db.flush()
        site = Site(isp_operator_id=operator.id, town_id=town.id, name="S", address="1 Test Road")
        db.add(site)
        await db.flush()
        router = Router(isp_operator_id=operator.id, site_id=site.id, name=f"R-{uuid.uuid4().hex[:6]}",
                        nas_identifier=f"nas-{uuid.uuid4().hex[:6]}", nas_secret=encrypt_secret("rt-secret"),
                        wg_tunnel_ip=f"10.100.{next(_TUNNEL)}.2", is_active=True, is_online=reachable)
        db.add(router)
        await db.flush()
        if credentials:
            db.add(RouterCredential(router_id=router.id, api_username="api",
                                    api_password_encrypted=encrypt_secret("api-pass")))
        await db.commit()
        return router


async def make_session(operator, router, *, open_=True) -> tuple["Session", "Voucher"]:
    tag = uuid.uuid4().hex[:8]
    async with async_session_factory() as db:
        plan = Plan(isp_operator_id=operator.id, name=f"P {tag}", type="time", download_speed_kbps=2048,
                    upload_speed_kbps=1024, price_ghs=Decimal("5"), duration_minutes=1440, is_active=True)
        db.add(plan)
        await db.flush()
        # vouchers.code is unique table-wide: tag it.
        voucher = Voucher(isp_operator_id=operator.id, plan_id=plan.id, code=f"HSE-{tag}", username=f"hse{tag}",
                          password=f"pw{tag}", status="active", expires_at=_now() + timedelta(hours=20))
        db.add(voucher)
        await db.flush()
        session = Session(isp_operator_id=operator.id, voucher_id=voucher.id, router_id=router.id,
                          username=voucher.username, ip_address="10.5.50.9", nas_ip=router.wg_tunnel_ip,
                          session_id=f"s-{tag}", started_at=_now() - timedelta(hours=3),
                          stopped_at=None if open_ else _now() - timedelta(hours=1))
        db.add(session)
        await db.commit()
        return session, voucher


async def fresh(model, row_id):
    async with async_session_factory() as db:
        return await db.get(model, row_id)


async def rows(model, *where):
    async with async_session_factory() as db:
        return (await db.execute(select(model).where(*where))).scalars().all()


async def ended_rows(operator):
    return await rows(AuditLog, AuditLog.target_id == str(operator.id), AuditLog.action == "operator.hotspot_service_ended")


def calls_for(env, router):
    return [c for c in env["coa_calls"] if c["router_ip"] == str(router.wg_tunnel_ip)]


# ── the job ───────────────────────────────────────────────────────────────

async def test_disconnects_only_the_ended_operators_open_sessions(env):
    ended = await make_operator("cancelled", ends_in=timedelta(minutes=-1))
    winding = await make_operator("cancelled", ends_in=timedelta(hours=2))
    other = await make_operator("approved")
    r_ended, r_winding, r_other = (await make_router(ended), await make_router(winding), await make_router(other))
    s1, v1 = await make_session(ended, r_ended)
    s2, v2 = await make_session(ended, r_ended)
    closed, _ = await make_session(ended, r_ended, open_=False)
    s_wind, _ = await make_session(winding, r_winding)
    s_other, _ = await make_session(other, r_other)

    stats = await hse.end_due_operators()

    assert stats["operators"] >= 1 and stats["errors"] == 0
    sent = calls_for(env, r_ended)
    assert sorted(c["attributes"]["User-Name"] for c in sent) == sorted([s1.username, s2.username])
    for call in sent:
        # VPN-only: the tunnel IP, the router's own secret, User-Name + Framed-IP-Address.
        assert call["router_ip"] == str(r_ended.wg_tunnel_ip) and call["router_secret"] == "rt-secret"
        assert call["attributes"]["Framed-IP-Address"] == "10.5.50.9"
    assert calls_for(env, r_winding) == [] and calls_for(env, r_other) == []
    events = await rows(CoAEvent, CoAEvent.isp_operator_id == ended.id)
    assert sorted(e.session_id for e in events) == sorted([s1.id, s2.id])
    assert all(e.status == "sent" and e.attempt_count == 1 and e.event_type == "disconnect" for e in events)
    assert sorted(env["revoked"][-2:]) == sorted([v1.id, v2.id])
    assert not await rows(CoAEvent, CoAEvent.isp_operator_id.in_([winding.id, other.id]))

    assert (await fresh(ISPOperator, ended.id)).hotspot_service_cut_off_at is not None
    assert (await fresh(ISPOperator, winding.id)).hotspot_service_cut_off_at is None
    assert (await fresh(ISPOperator, other.id)).hotspot_service_cut_off_at is None
    (row,) = await ended_rows(ended)
    assert (row.actor_type, row.isp_operator_id, row.target_type) == ("system", ended.id, "isp_operator")
    assert row.detail["sessions"] == 2 and row.detail["coa_sent"] == 2 and row.detail["coa_failed"] == 0
    assert row.detail["active_removed"] == 2 and row.detail["cookies_cleared"] == 4
    assert not await ended_rows(winding) and not await ended_rows(other)
    # The job never closes rows itself: the router's Accounting-Stop does.
    assert (await fresh(Session, s1.id)).stopped_at is None


async def test_runs_once_per_wind_down(env):
    operator = await make_operator("cancelled", ends_in=timedelta(minutes=-5))
    router = await make_router(operator)
    await make_session(operator, router)
    await hse.end_due_operators()
    first = len(calls_for(env, router))
    await hse.end_due_operators()
    assert len(calls_for(env, router)) == first == 1
    assert len(await ended_rows(operator)) == 1
    assert len(await rows(CoAEvent, CoAEvent.isp_operator_id == operator.id)) == 1


async def test_operator_with_nobody_online_still_gets_the_mark_and_the_row(env):
    operator = await make_operator("cancelled", ends_in=timedelta(seconds=-1))
    await make_router(operator)
    await hse.end_due_operators()
    assert (await fresh(ISPOperator, operator.id)).hotspot_service_cut_off_at is not None
    (row,) = await ended_rows(operator)
    assert row.detail["sessions"] == 0


async def test_failed_coa_is_counted_and_left_for_coa_retry(env):
    operator = await make_operator("cancelled", ends_in=timedelta(minutes=-1))
    router = await make_router(operator)
    session, _ = await make_session(operator, router)
    env["coa_result"].update(status="failed", message="timeout")
    try:
        await hse.end_due_operators()
    finally:
        env["coa_result"].clear()
        env["coa_result"]["status"] = "success"
    (event,) = await rows(CoAEvent, CoAEvent.session_id == session.id)
    # coa_retry picks up status failed, attempt_count < 3, last attempt > 2 min ago.
    assert event.status == "failed" and event.attempt_count == 1 and event.last_attempted_at is not None
    (row,) = await ended_rows(operator)
    assert row.detail["coa_failed"] == 1 and row.detail["coa_sent"] == 0


async def test_wind_down_operator_is_cut_off_once_its_end_passes(env):
    operator = await make_operator("cancelled", ends_in=timedelta(hours=1))
    router = await make_router(operator)
    await make_session(operator, router)
    await hse.end_due_operators()
    assert calls_for(env, router) == [] and not await ended_rows(operator)
    async with async_session_factory() as db:
        await db.execute(update(ISPOperator).where(ISPOperator.id == operator.id)
                         .values(hotspot_service_ends_at=_now() - timedelta(seconds=1)))
        await db.commit()
    await hse.end_due_operators()
    assert len(calls_for(env, router)) == 1 and len(await ended_rows(operator)) == 1


async def test_restore_while_routers_are_contacted_leaves_no_mark_and_no_row(env):
    operator = await make_operator("cancelled", ends_in=timedelta(minutes=-1))
    router = await make_router(operator)
    await make_session(operator, router)

    def restore_now():
        # The platform owner restores between the job's read and its mark.
        import psycopg2
        url = os.environ["SYNC_DATABASE_URL"] if os.getenv("SYNC_DATABASE_URL") else FLOW_DB.replace("+asyncpg", "")
        with psycopg2.connect(url) as conn, conn.cursor() as cur:
            cur.execute("UPDATE isp_operators SET status='approved', billing_status='active', cancelled_at=NULL, "
                        "hotspot_service_ends_at=NULL WHERE id=%s", (str(operator.id),))

    env["during_coa"] = restore_now
    try:
        stats = await hse.end_due_operators()
    finally:
        env.pop("during_coa")
    after = await fresh(ISPOperator, operator.id)
    assert after.status == "approved" and after.hotspot_service_cut_off_at is None
    assert not await ended_rows(operator)
    # A clean run, not one rolled back by 059's CHECK: the disconnect that was
    # sent stands, with its coa_events row (they can log in again at once).
    assert stats["errors"] == 0
    assert len(calls_for(env, router)) == 1
    (event,) = await rows(CoAEvent, CoAEvent.isp_operator_id == operator.id)
    assert event.status == "sent"


# ── restore, migration 059 ────────────────────────────────────────────────

async def test_restore_after_the_cut_off_clears_the_mark(env):
    operator = await make_operator("cancelled", ends_in=timedelta(minutes=-1))
    await make_router(operator)
    await hse.end_due_operators()
    assert (await fresh(ISPOperator, operator.id)).hotspot_service_cut_off_at is not None
    res = await env["client"].put(f"/platform/operators/{operator.id}/status", headers=env["headers"],
                                  json={"status": "approved"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["hotspot_service_cut_off_at"] is None and body["hotspot_service_ends_at"] is None
    (row,) = await rows(AuditLog, AuditLog.target_id == str(operator.id), AuditLog.action == "operator.status_change")
    assert "hotspot_service_cut_off_at" in row.changes


async def test_cut_off_mark_needs_an_end_date(env):
    operator = await make_operator("approved")
    async with async_session_factory() as db:
        with pytest.raises(IntegrityError, match="ck_isp_operators_cut_off"):
            await db.execute(update(ISPOperator).where(ISPOperator.id == operator.id)
                             .values(hotspot_service_cut_off_at=_now()))
            await db.commit()


# ── End hotspot service now ───────────────────────────────────────────────

async def end_now(env, operator, **body):
    return await env["client"].post(f"/platform/operators/{operator.id}/hotspot-service/end",
                                    headers=env["headers"], json=body)


async def test_end_now_brings_the_end_forward_and_the_job_cuts_off(env):
    operator = await make_operator("cancelled", ends_in=timedelta(days=20))
    router = await make_router(operator)
    await make_session(operator, router)
    due = (await fresh(ISPOperator, operator.id)).hotspot_service_ends_at

    res = await end_now(env, operator, reason=REASON)

    assert res.status_code == 200, res.text
    after = await fresh(ISPOperator, operator.id)
    assert after.status == "cancelled" and after.billing_status == "cancelled"
    assert abs((after.hotspot_service_ends_at - _now()).total_seconds()) < 30
    assert after.hotspot_service_cut_off_at is None  # the job does that
    (row,) = await rows(AuditLog, AuditLog.target_id == str(operator.id),
                        AuditLog.action == "operator.hotspot_service_end_now")
    assert row.actor_type == "platform_owner"
    assert row.detail["reason"] == REASON and row.detail["was_due"] == due.isoformat()
    assert "hotspot_service_ends_at" in row.changes

    # The reason stays internal: the operator's own audit view drops it.
    view = await env["client"].get("/platform/audit", headers=env["headers"],
                                   params={"action": "operator.hotspot_service_end_now", "operator": str(operator.id)})
    assert view.status_code == 200 and REASON in view.text
    from src.modules.audit import query as audit_query
    shown = audit_query.present_row(_with_name(row, operator), viewer="operator")
    assert REASON not in str(shown)

    await hse.end_due_operators()
    assert len(calls_for(env, router)) == 1 and len(await ended_rows(operator)) == 1
    assert (await end_now(env, operator)).status_code == 409  # already ended


def _with_name(row, operator):
    from types import SimpleNamespace
    return SimpleNamespace(**{c.name: getattr(row, c.name) for c in AuditLog.__table__.columns},
                           operator_name=operator.name)


@pytest.mark.parametrize("status", ["approved", "suspended", "pending"])
async def test_end_now_refused_unless_cancelled(env, status):
    operator = await make_operator(status) if status != "suspended" else await _suspended()
    res = await end_now(env, operator)
    assert res.status_code == 409 and "not cancelled" in res.json()["detail"]
    assert (await fresh(ISPOperator, operator.id)).hotspot_service_ends_at is None


async def _suspended():
    slug = f"hse-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Hse {slug}", slug=slug, contact_email=f"{slug}@throwaway.test",
                               status="suspended", suspension_reason="manual", billing_status="paused")
        db.add(operator)
        await db.commit()
        return operator


async def test_end_now_after_the_end_has_passed_is_refused(env):
    operator = await make_operator("cancelled", ends_in=timedelta(minutes=-10))
    before = (await fresh(ISPOperator, operator.id)).hotspot_service_ends_at
    res = await end_now(env, operator)
    assert res.status_code == 409 and "already ended" in res.json()["detail"]
    assert (await fresh(ISPOperator, operator.id)).hotspot_service_ends_at == before


async def test_end_now_validates_its_body_and_the_operator(env):
    operator = await make_operator("cancelled", ends_in=timedelta(days=3))
    assert (await end_now(env, operator, reason="x" * 501)).status_code == 422
    assert (await end_now(env, operator, days=0)).status_code == 422
    missing = await env["client"].post(f"/platform/operators/{uuid.uuid4()}/hotspot-service/end",
                                       headers=env["headers"], json={})
    assert missing.status_code == 404
    unauthenticated = await env["client"].post(f"/platform/operators/{operator.id}/hotspot-service/end", json={})
    assert unauthenticated.status_code in (401, 403)
    assert (await fresh(ISPOperator, operator.id)).hotspot_service_ends_at > _now()


# ── reconcile_hotspot during and after the wind-down ──────────────────────

class FakeRouterApi:
    """RouterOS as reconcile_hotspot sees it: per router, who is online."""

    def __init__(self, online: dict[str, list[str]]):
        self.online = online
        self.removed: list[tuple[str, list[str]]] = []

    async def enforce_cookie_policy(self, router_id):
        return []

    async def get_active_hotspot_users(self, router_id):
        return [ActiveUserInfo(id=f"*{i}", user=name, address="10.5.50.9", mac_address="A4:1F:72:63:11:54",
                               uptime="3h")
                for i, name in enumerate(self.online.get(router_id, []))]

    async def get_hotspot_cookies(self, router_id):
        return []

    async def remove_hotspot_user_access(self, router_id, usernames, mac=None):
        self.removed.append((router_id, list(usernames)))
        return {"active_removed": 1, "cookies_cleared": 0}

    async def clear_hotspot_cookies(self, router_id, usernames, mac=None):
        return 0


async def test_reconcile_leaves_a_wind_down_operators_active_session_alone_until_the_end(env, monkeypatch):
    winding = await make_operator("cancelled", ends_in=timedelta(hours=2))
    other = await make_operator("approved")
    r_wind = await make_router(winding, reachable=True, credentials=True)
    r_other = await make_router(other, reachable=True, credentials=True)
    s_wind, _ = await make_session(winding, r_wind)
    s_other, _ = await make_session(other, r_other)
    api = FakeRouterApi({str(r_wind.id): [s_wind.username], str(r_other.id): [s_other.username]})
    monkeypatch.setattr(rh, "MikroTikAPIService", lambda: api)

    stats = await rh.reconcile_hotspot(router_ids=[str(r_wind.id), str(r_other.id)])

    assert stats["routers_checked"] == 2 and stats["router_errors"] == 0
    assert api.removed == [] and stats["revoked_online"] == 0
    assert (await fresh(Session, s_wind.id)).stopped_at is None   # survives the run
    assert (await fresh(Session, s_other.id)).stopped_at is None

    async with async_session_factory() as db:
        await db.execute(update(ISPOperator).where(ISPOperator.id == winding.id)
                         .values(hotspot_service_ends_at=_now() - timedelta(seconds=1)))
        await db.commit()
    stats = await rh.reconcile_hotspot(router_ids=[str(r_wind.id), str(r_other.id)])

    assert api.removed == [(str(r_wind.id), [s_wind.username])]
    assert stats["revoked_online"] == 1
    assert (await fresh(Session, s_other.id)).stopped_at is None   # the other operator untouched
