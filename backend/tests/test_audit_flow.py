"""audit_log against a REAL database: the append-only rules, the transaction
contract, and the rows that sign-in, sign-out and the security events write.

Runs the FastAPI app in-process, like tests/test_admin_logout_flow.py. Only
runs against a disposable database at migration 055 or later:

    AUDIT_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("AUDIT_FLOW_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs AUDIT_FLOW_TEST_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import func, select, text, update
    from sqlalchemy.exc import DBAPIError

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import AdminSecurityEvent, AdminUser, AuditLog, ISPOperator
    from src.middleware import auth as auth_middleware
    from src.modules import audit
    from src.modules.admin_accounts import lockout
    from src.modules.admin_accounts import routes as account_routes
    from src.modules.auth import routes as auth_routes
    from src.utils.auth import hash_password

PASSWORD = "Str0ngPassword"
PIN = "4729"
PHONE = "233244000777"
UA = "audit-flow-test/1.0"


@pytest_asyncio.fixture(loop_scope="module")
async def client(monkeypatch):
    async def no_limit(key, bucket, limit=10, window_seconds=60):
        return None

    for module in (account_routes, auth_routes, auth_middleware):
        monkeypatch.setattr(module, "enforce_rate_limit", no_limit)

    async def no_lockout_sms(*args, **kwargs):
        return None

    monkeypatch.setattr(lockout, "send_lockout_notification", no_lockout_sms)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver/api/v1", headers={"User-Agent": UA}
    ) as c:
        yield c


async def make_admin(*, pin: str | None = None, **values) -> "AdminUser":
    slug = f"aud-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Aud {slug}", slug=slug, contact_email=f"{slug}@throwaway.test", status="approved")
        db.add(operator)
        await db.flush()
        state = dict(
            isp_operator_id=operator.id, email=f"{slug}@throwaway.test", password_hash=hash_password(PASSWORD),
            role="superadmin", is_active=True, phone=PHONE, phone_verified=True, must_complete_onboarding=False,
            must_change_password=False, security_question="birth_town", security_answer_hash=hash_password("accra"),
            pin_hash=hash_password(pin) if pin else None, pin_set_at=datetime.now(timezone.utc) if pin else None,
        )
        state.update(values)
        admin = AdminUser(**state)
        db.add(admin)
        await db.commit()
        return admin


async def rows_for(admin, action: str | None = None) -> list:
    async with async_session_factory() as db:
        query = select(AuditLog).where(AuditLog.target_id == str(admin.id))
        if action:
            query = query.where(AuditLog.action == action)
        return (await db.execute(query.order_by(AuditLog.created_at, AuditLog.id))).scalars().all()


async def count(action: str) -> int:
    async with async_session_factory() as db:
        return (await db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.action == action))).scalar_one()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def sign_in(client, admin) -> dict:
    res = await client.post("/auth/login", json={"email": admin.email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()


# ── The table's own rules ─────────────────────────────────────────────────


async def _one_row() -> uuid.UUID:
    async with async_session_factory() as db:
        row = audit.record(db, "test.row", actor=audit.Actor.system())
        await db.commit()
        return row.id


async def test_update_is_refused():
    row_id = await _one_row()
    async with async_session_factory() as db:
        with pytest.raises(DBAPIError, match="append-only: UPDATE refused"):
            await db.execute(update(AuditLog).where(AuditLog.id == row_id).values(action="tampered"))


async def test_delete_is_refused_without_the_purge_setting():
    row_id = await _one_row()
    async with async_session_factory() as db:
        with pytest.raises(DBAPIError, match="append-only: DELETE refused"):
            await db.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": row_id})


async def test_truncate_is_refused():
    async with async_session_factory() as db:
        with pytest.raises(DBAPIError, match="append-only: TRUNCATE refused"):
            await db.execute(text("TRUNCATE audit_log"))


async def test_purge_setting_allows_delete_in_that_transaction_only():
    row_id = await _one_row()
    async with async_session_factory() as db:
        await db.execute(text("SET LOCAL audit.purge = 'on'"))
        deleted = await db.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": row_id})
        assert deleted.rowcount == 1
        await db.commit()
    # SET LOCAL ended with that transaction: the next one is refused again.
    other = await _one_row()
    async with async_session_factory() as db:
        with pytest.raises(DBAPIError, match="DELETE refused"):
            await db.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": other})


async def test_unknown_actor_type_is_refused():
    async with async_session_factory() as db:
        db.add(AuditLog(actor_type="nobody", action="x"))
        with pytest.raises(DBAPIError, match="ck_audit_log_actor_type"):
            await db.commit()


# ── The transaction contract ──────────────────────────────────────────────


async def test_a_rolled_back_action_leaves_no_row():
    marker = f"test.rollback.{uuid.uuid4().hex[:8]}"
    async with async_session_factory() as db:
        audit.record(db, marker, actor=audit.Actor.system())
        await db.rollback()
    assert await count(marker) == 0


async def test_absent_changes_and_detail_are_sql_null():
    """Not the JSON value null: a filter on "changes IS NULL" must find these
    rows, on both write paths."""
    marker = f"test.nulls.{uuid.uuid4().hex[:8]}"
    async with async_session_factory() as db:
        audit.record(db, marker, actor=audit.Actor.system())
        await db.commit()
    await audit.record_detached(marker, actor=audit.Actor.system())
    async with async_session_factory() as db:
        counts = (await db.execute(text(
            "SELECT count(*) FILTER (WHERE changes IS NULL AND detail IS NULL), "
            "count(*) FILTER (WHERE changes = 'null'::jsonb OR detail = 'null'::jsonb) "
            "FROM audit_log WHERE action = :action"
        ), {"action": marker})).one()
    assert tuple(counts) == (2, 0)


async def test_detached_write_never_raises():
    marker = f"test.detached.{uuid.uuid4().hex[:8]}"
    await audit.record_detached(marker, actor=audit.Actor("nobody"))  # CHECK fails inside
    assert await count(marker) == 0
    await audit.record_detached(marker, actor=audit.Actor.system())
    assert await count(marker) == 1


# ── Sign-in ───────────────────────────────────────────────────────────────


async def test_successful_sign_in_is_audited_with_actor_and_request(client):
    admin = await make_admin()
    res = await client.post(
        "/auth/login", json={"email": admin.email, "password": PASSWORD}, headers={"X-Request-ID": "flow-req-1"}
    )
    assert res.status_code == 200
    [row] = await rows_for(admin, "auth.login")
    assert (row.actor_type, row.actor_id, row.actor_label) == ("operator_user", admin.id, admin.email)
    assert row.isp_operator_id == admin.isp_operator_id
    assert (row.target_type, row.user_agent, row.request_id) == ("admin_user", UA, "flow-req-1")
    assert str(row.ip) == "127.0.0.1"


async def test_wrong_password_is_audited_and_answers_like_an_unknown_email(client):
    admin = await make_admin()
    wrong = await client.post("/auth/login", json={"email": admin.email, "password": "nope-nope-1"})
    unknown = await client.post("/auth/login", json={"email": f"ghost-{uuid.uuid4().hex}@throwaway.test", "password": "x"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.content == unknown.content
    assert wrong.headers["content-type"] == unknown.headers["content-type"]

    [row] = await rows_for(admin, "auth.login_failed")
    assert (row.actor_type, row.actor_id) == ("system", None)
    assert row.isp_operator_id == admin.isp_operator_id
    assert row.detail == {"reason": "wrong_password"}
    assert row.user_agent == UA and str(row.ip) == "127.0.0.1"
    assert "nope-nope-1" not in repr((row.summary, row.detail, row.changes))


async def test_unknown_email_writes_nothing(client):
    ghost = f"ghost-{uuid.uuid4().hex}@throwaway.test"
    before = await count("auth.login_failed")
    res = await client.post("/auth/login", json={"email": ghost, "password": "whatever-1"})
    assert res.status_code == 401
    assert await count("auth.login_failed") == before
    async with async_session_factory() as db:
        hits = (await db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.summary.contains(ghost)))).scalar_one()
    assert hits == 0


async def test_disabled_account_counts_as_unknown(client):
    """P1 behaviour: login never loads an inactive account, so its failures
    are indistinguishable from an unknown email and write nothing. P2 may
    revisit this."""
    admin = await make_admin(is_active=False, disabled_at=datetime.now(timezone.utc), disabled_by_type="system")
    res = await client.post("/auth/login", json={"email": admin.email, "password": "nope-nope-1"})
    assert res.status_code == 401
    assert await rows_for(admin) == []


async def test_lockout_writes_one_lockout_row_and_a_failure_per_attempt(client):
    admin = await make_admin()
    for _ in range(lockout.LOGIN_MAX_ATTEMPTS):
        res = await client.post("/auth/login", json={"email": admin.email, "password": "nope-nope-1"})
        assert res.status_code == 401
    # Locked now: even the right password is refused, and audited as such.
    res = await client.post("/auth/login", json={"email": admin.email, "password": PASSWORD})
    assert res.status_code == 401

    failures = await rows_for(admin, "auth.login_failed")
    assert [r.detail["reason"] for r in failures] == ["wrong_password"] * lockout.LOGIN_MAX_ATTEMPTS + ["locked"]
    [locked] = await rows_for(admin, "security.login_lockout")
    assert locked.actor_type == "system"
    assert locked.detail["attempts"] == lockout.LOGIN_MAX_ATTEMPTS
    assert await rows_for(admin, "auth.login") == []


# ── Sign-out ──────────────────────────────────────────────────────────────


async def test_sign_out_is_audited_once(client):
    admin = await make_admin()
    tokens = await sign_in(client, admin)
    assert (await client.post("/auth/logout", headers=bearer(tokens["access_token"]))).status_code == 204
    # A replay has no session left to end: no second row.
    assert (await client.post("/auth/logout", headers=bearer(tokens["access_token"]))).status_code == 204

    [row] = await rows_for(admin, "auth.logout")
    assert (row.actor_type, row.actor_id, row.actor_label) == ("operator_user", admin.id, admin.email)
    assert row.isp_operator_id == admin.isp_operator_id
    assert row.detail == {"via": "access"}
    async with async_session_factory() as db:
        events = (await db.execute(select(AdminSecurityEvent).where(
            AdminSecurityEvent.admin_user_id == admin.id, AdminSecurityEvent.event_type == "signed_out"
        ))).scalars().all()
    assert len(events) == 1


# ── Security events ───────────────────────────────────────────────────────


async def test_pin_set_mirrors_the_security_event_without_the_pin(client):
    admin = await make_admin()
    tokens = await sign_in(client, admin)
    res = await client.post(
        "/auth/me/pin", headers=bearer(tokens["access_token"]),
        json={"current_password": PASSWORD, "new_pin": PIN, "confirm_pin": PIN},
    )
    assert res.status_code == 200, res.text
    [row] = await rows_for(admin, "security.pin_set")
    assert (row.actor_type, row.actor_id) == ("operator_user", admin.id)
    assert row.detail == {"client_ip": "127.0.0.1"}
    assert PIN not in repr((row.summary, row.detail, row.changes))


async def test_a_refused_change_writes_no_security_row(client):
    admin = await make_admin()
    tokens = await sign_in(client, admin)
    res = await client.post(
        "/auth/me/pin", headers=bearer(tokens["access_token"]),
        json={"current_password": PASSWORD, "new_pin": PIN, "confirm_pin": "9999"},
    )
    assert res.status_code == 400
    assert await rows_for(admin, "security.pin_set") == []


async def test_pin_lockout_is_attributed_to_the_signed_in_admin(client):
    admin = await make_admin(pin=PIN)
    tokens = await sign_in(client, admin)
    for _ in range(lockout.PIN_MAX_ATTEMPTS):
        await client.post("/auth/me/pin/verify", headers=bearer(tokens["access_token"]), json={"pin": "0000"})
    [row] = await rows_for(admin, "security.pin_lockout")
    assert (row.actor_type, row.actor_id, row.actor_label) == ("operator_user", admin.id, admin.email)
    assert row.detail["attempts"] == lockout.PIN_MAX_ATTEMPTS


async def test_security_question_change_is_audited(client):
    admin = await make_admin(pin=PIN)
    tokens = await sign_in(client, admin)
    verified = await client.post("/auth/me/pin/verify", headers=bearer(tokens["access_token"]), json={"pin": PIN})
    assert verified.status_code == 200, verified.text
    res = await client.post(
        "/auth/me/security-question", headers=bearer(tokens["access_token"]),
        json={"current_password": PASSWORD, "security_question": "first_school", "security_answer": "Achimota"},
    )
    assert res.status_code == 200, res.text
    [row] = await rows_for(admin, "security.security_question_changed")
    assert row.actor_id == admin.id
    assert "Achimota" not in repr((row.summary, row.detail, row.changes))


async def test_an_earlier_requests_actor_never_leaks_into_a_later_one(client):
    """Admin A's authenticated request binds A as the actor; a failed sign-in
    for B straight after must still be a system row, not A's."""
    first = await make_admin()
    second = await make_admin()
    tokens = await sign_in(client, first)
    assert (await client.get("/auth/me/security", headers=bearer(tokens["access_token"]))).status_code == 200
    res = await client.post("/auth/login", json={"email": second.email, "password": "nope-nope-1"})
    assert res.status_code == 401
    [row] = await rows_for(second, "auth.login_failed")
    assert (row.actor_type, row.actor_id) == ("system", None)
