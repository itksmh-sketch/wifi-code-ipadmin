"""P5: the audit pages' API and the retention job, against a REAL database.

  * an operator only ever sees its own rows: no filter, cursor or page size
    reaches another operator's;
  * only a superadmin reads it (admin and staff: 403);
  * the operator view never shows a platform owner's email, id, IP, user agent
    or typed reason; the platform view shows them;
  * both default to people; system rows only on request;
  * keyset pagination is stable: every row once, in order, even with tied
    timestamps and rows arriving between pages;
  * JSON null in changes/detail reads as no value;
  * the nightly purge removes only rows past retention, in batches, inside
    the audit.purge window, and records each batch.

Only runs against a disposable database:

    AUDIT_PAGES_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>

(tests/throwaway_db.py run sets both.)
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("AUDIT_PAGES_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs AUDIT_PAGES_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:
    import httpx
    from sqlalchemy import select, text

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import AdminUser, AuditLog, ISPOperator, PlatformOwner
    from src.modules.auth.tokens import platform_owner_token_data
    from src.utils.auth import create_platform_owner_access_token, hash_password

PASSWORD = "Str0ngPassword"
TYPED_REASON = "Fraud complaint from the bank, call Kofi on 0244111222"


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    async def no_limit(*a, **kw):
        return None

    for name, module in list(sys.modules.items()):
        if name.startswith("src.") and hasattr(module, "enforce_rate_limit"):
            monkeypatch.setattr(module, "enforce_rate_limit", no_limit)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as client:
        yield {"client": client}


async def make_operator():
    slug = f"aud5-{uuid.uuid4().hex[:8]}"
    async with async_session_factory() as db:
        op = ISPOperator(name=f"Op {slug}", slug=slug, contact_email=f"{slug}@throwaway.test", status="approved")
        db.add(op)
        await db.commit()
        return op


async def make_admin(operator, role):
    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        admin = AdminUser(
            isp_operator_id=operator.id, email=f"{role}-{uuid.uuid4().hex[:8]}@throwaway.test",
            password_hash=hash_password(PASSWORD), role=role, is_active=True, phone="233244000777",
            phone_verified=True, must_complete_onboarding=False, must_change_password=False,
            pin_hash=hash_password("482913"), pin_set_at=now, pin_verified_until=now + timedelta(minutes=10),
        )
        db.add(admin)
        await db.commit()
        return admin


async def headers_for(env, admin):
    res = await env["client"].post("/auth/login", json={"email": admin.email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


async def seed(operator_id, *, actor_type, action, created_at=None, actor=None, label=None, detail=None,
               changes=None, ip="198.51.100.7", user_agent="Mozilla/5.0 test"):
    async with async_session_factory() as db:
        row = AuditLog(
            isp_operator_id=operator_id, actor_type=actor_type, actor_id=actor, actor_label=label, action=action,
            target_type="thing", target_id=str(uuid.uuid4()), summary=f"{action} happened", changes=changes,
            detail=detail, ip=ip if actor_type != "system" else None, user_agent=user_agent if actor_type != "system" else None,
        )
        if created_at is not None:
            row.created_at = created_at
        db.add(row)
        await db.commit()
        return row


@pytest_asyncio.fixture(loop_scope="module")
async def world(env):
    """Operators A and B, each with a superadmin, people's rows and system rows,
    and one platform-owner action on A carrying a typed reason."""
    async with async_session_factory() as db:
        owner = PlatformOwner(email=f"owner-{uuid.uuid4().hex[:10]}@throwaway.test", password_hash=hash_password(PASSWORD), name="Owner",
                              is_active=True, must_complete_security_setup=False)
        db.add(owner)
        await db.commit()
    a, b = await make_operator(), await make_operator()
    sa, sb = await make_admin(a, "superadmin"), await make_admin(b, "superadmin")
    base = datetime.now(timezone.utc) - timedelta(days=3)
    ids = {"a": set(), "a_system": set(), "b": set()}
    for i in range(6):
        when = base + timedelta(hours=i)
        ids["a"].add((await seed(a.id, actor_type="operator_user", actor=sa.id, label=sa.email,
                                 action="plan.update" if i % 2 else "voucher.disable", created_at=when)).id)
        ids["b"].add((await seed(b.id, actor_type="operator_user", actor=sb.id, label=sb.email,
                                 action="plan.update", created_at=when)).id)
        ids["a_system"].add((await seed(a.id, actor_type="system", action="voucher.expire", created_at=when)).id)
        ids["b"].add((await seed(b.id, actor_type="system", action="voucher.expire", created_at=when)).id)
    platform_row = await seed(a.id, actor_type="platform_owner", actor=owner.id, label=owner.email,
                              action="admin_user.disable", detail={"reason": TYPED_REASON, "admins_signed_out": 1},
                              created_at=base + timedelta(hours=7), ip="203.0.113.9", user_agent="OwnerBrowser/1")
    ids["a"].add(platform_row.id)
    return {"a": a, "b": b, "sa": sa, "sb": sb, "owner": owner, "ids": ids, "platform_row": platform_row,
            "ha": await headers_for(env, sa), "hb": await headers_for(env, sb),
            "ho": {"Authorization": "Bearer " + create_platform_owner_access_token(platform_owner_token_data(owner))}}


async def all_pages(env, path, headers, params=None, limit=4):
    """Every row a client can reach by following next_cursor."""
    seen, cursor = [], None
    for _ in range(200):
        q = {**(params or {}), "limit": limit, **({"cursor": cursor} if cursor else {})}
        res = await env["client"].get(path, headers=headers, params=q)
        assert res.status_code == 200, res.text
        body = res.json()
        seen.extend(body["rows"])
        cursor = body["next_cursor"]
        if not cursor:
            return seen
    raise AssertionError("pagination never ended")


def ids_of(rows):
    return {uuid.UUID(r["id"]) for r in rows}


async def db_ids(operator_id, *, system=False, people=True):
    """The truth: every row the database holds for one operator (signing in
    writes real auth.login rows on top of the seeded ones)."""
    kinds = [*(["operator_user", "platform_owner"] if people else []), *(["system"] if system else [])]
    async with async_session_factory() as db:
        return set((await db.execute(select(AuditLog.id).where(
            AuditLog.isp_operator_id == operator_id, AuditLog.actor_type.in_(kinds)))).scalars())


# ── Access ───────────────────────────────────────────────────────────────


async def test_only_a_superadmin_reads_the_operator_log(env, world):
    for role in ("admin", "staff"):
        h = await headers_for(env, await make_admin(world["a"], role))
        res = await env["client"].get("/admin/audit", headers=h)
        assert res.status_code == 403, (role, res.text)
    assert (await env["client"].get("/admin/audit", headers=world["ha"])).status_code == 200
    assert (await env["client"].get("/admin/audit")).status_code in (401, 403)
    # An operator token is not a platform owner's, and the reverse.
    assert (await env["client"].get("/platform/audit", headers=world["ha"])).status_code in (401, 403)
    assert (await env["client"].get("/admin/audit", headers=world["ho"])).status_code in (401, 403)


# ── Tenant isolation ─────────────────────────────────────────────────────


async def test_operator_a_never_sees_b_through_any_filter_or_cursor(env, world):
    c, h, ids = env["client"], world["ha"], world["ids"]
    people = ids_of(await all_pages(env, "/admin/audit", h))
    assert people == await db_ids(world["a"].id) and ids["a"] <= people
    everything = ids_of(await all_pages(env, "/admin/audit", h, {"include_system": "true"}))
    assert everything == await db_ids(world["a"].id, system=True) and ids["a"] | ids["a_system"] <= everything
    b_ids = await db_ids(world["b"].id, system=True)
    assert ids["b"] <= b_ids
    a_all = everything

    probes = [
        {"user": str(world["sb"].id)},                       # B's own admin
        {"user": str(world["sb"].id), "include_system": "true"},
        {"user": "system", "include_system": "true"},
        {"user": "platform"},
        {"action": "plan"}, {"action": "voucher"}, {"action": "%"}, {"action": "_"},
        {"date_from": "2000-01-01", "date_to": "2100-01-01", "include_system": "true"},
        {"operator": str(world["b"].id)},                    # not a parameter here: ignored, never obeyed
        {"isp_operator_id": str(world["b"].id)},
    ]
    for params in probes:
        got = ids_of(await all_pages(env, "/admin/audit", h, params))
        assert not (got & b_ids), params
        assert got <= a_all, params
    assert ids_of(await all_pages(env, "/admin/audit", h, {"user": str(world["sb"].id)})) == set()

    # A cursor made from B's newest row is only a position: A's rows older than it.
    async with async_session_factory() as db:
        newest_b = (await db.execute(select(AuditLog).where(AuditLog.isp_operator_id == world["b"].id)
                                     .order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(1))).scalar_one()
    from src.modules.audit.query import encode_cursor

    res = await c.get("/admin/audit", headers=h, params={"cursor": encode_cursor(newest_b.created_at, newest_b.id),
                                                        "include_system": "true", "limit": 200})
    assert res.status_code == 200
    assert ids_of(res.json()["rows"]) <= a_all and not (ids_of(res.json()["rows"]) & b_ids)

    for bad in ({"cursor": "not-a-cursor"}, {"user": "not-a-uuid"}, {"limit": 0}, {"limit": 201}):
        assert (await c.get("/admin/audit", headers=h, params=bad)).status_code in (400, 422), bad


# ── What an operator is shown of a platform action ───────────────────────


async def test_operator_view_hides_who_and_why_of_platform_actions(env, world):
    res = await env["client"].get("/admin/audit", headers=world["ha"], params={"user": "platform"})
    assert res.status_code == 200
    [row] = res.json()["rows"]
    assert row["actor"] == {"type": "platform_owner", "id": None, "label": "Platform"}
    assert row["detail"] == {"admins_signed_out": 1}
    assert row["ip"] is None and row["user_agent"] is None
    everything = json.dumps(await all_pages(env, "/admin/audit", world["ha"], {"include_system": "true"}))
    for leak in (world["owner"].email, str(world["owner"].id), TYPED_REASON, "0244111222", "203.0.113.9", "OwnerBrowser"):
        assert leak not in everything, leak

    # The platform view shows all of it.
    res = await env["client"].get("/platform/audit", headers=world["ho"],
                                  params={"operator": str(world["a"].id), "actor_type": "platform_owner"})
    [prow] = res.json()["rows"]
    assert prow["actor"]["label"] == world["owner"].email and prow["detail"]["reason"] == TYPED_REASON
    assert prow["ip"] == "203.0.113.9" and prow["operator"] == {"id": str(world["a"].id), "name": world["a"].name}


# ── Platform view ────────────────────────────────────────────────────────


async def test_platform_view_filters_and_defaults_to_people(env, world):
    ho, a, b = world["ho"], world["a"], world["b"]
    rows_a = await all_pages(env, "/platform/audit", ho, {"operator": str(a.id)})
    assert ids_of(rows_a) == await db_ids(a.id) and all(r["actor"]["type"] != "system" for r in rows_a)
    rows_b_sys = await all_pages(env, "/platform/audit", ho, {"operator": str(b.id), "actor_type": "system"})
    assert rows_b_sys and all(r["actor"]["type"] == "system" for r in rows_b_sys)
    with_sys = ids_of(await all_pages(env, "/platform/audit", ho, {"operator": str(a.id), "include_system": "true"}))
    assert with_sys == await db_ids(a.id, system=True)
    wide = await all_pages(env, "/platform/audit", ho, {"operator": "none", "include_system": "true"})
    assert all(r["operator"] is None for r in wide)
    plans = await all_pages(env, "/platform/audit", ho, {"operator": str(b.id), "action": "plan."})
    assert plans and all(r["action"].startswith("plan.") for r in plans)
    for bad in ({"operator": "nope"}, {"actor_type": "robot"}, {"cursor": "%%%"}):
        assert (await env["client"].get("/platform/audit", headers=ho, params=bad)).status_code == 400, bad


# ── Pagination ───────────────────────────────────────────────────────────


async def test_cursor_pagination_is_stable_with_ties_and_new_rows(env):
    op = await make_operator()
    sa = await make_admin(op, "superadmin")
    h = await headers_for(env, sa)
    tie = datetime.now(timezone.utc) - timedelta(hours=1)
    seeded = [(await seed(op.id, actor_type="operator_user", actor=sa.id, label=sa.email, action="plan.update",
                          created_at=tie)).id for _ in range(7)]
    seeded += [(await seed(op.id, actor_type="operator_user", actor=sa.id, label=sa.email, action="plan.update",
                           created_at=tie - timedelta(minutes=m))).id for m in (1, 2, 3)]

    first = await env["client"].get("/admin/audit", headers=h, params={"limit": 3, "action": "plan."})
    got = first.json()["rows"]
    # A row arriving mid-way is newer than every cursor: it never shifts later pages.
    late = await seed(op.id, actor_type="operator_user", actor=sa.id, label=sa.email, action="plan.update")
    cursor = first.json()["next_cursor"]
    while cursor:
        res = await env["client"].get("/admin/audit", headers=h, params={"limit": 3, "action": "plan.", "cursor": cursor})
        got += res.json()["rows"]
        cursor = res.json()["next_cursor"]

    keys = [(r["created_at"], r["id"]) for r in got]
    assert [uuid.UUID(r["id"]) for r in got if r["id"] != str(late.id)] == [uuid.UUID(r["id"]) for r in got]
    assert len(keys) == len(set(keys)) == 10 and set(ids_of(got)) == set(seeded)
    stamps = [(datetime.fromisoformat(r["created_at"]), uuid.UUID(r["id"])) for r in got]
    assert stamps == sorted(stamps, reverse=True)


# ── JSON null ────────────────────────────────────────────────────────────


async def test_json_null_reads_as_no_value(env):
    op = await make_operator()
    sa = await make_admin(op, "superadmin")
    async with async_session_factory() as db:
        await db.execute(text(
            "INSERT INTO audit_log (isp_operator_id, actor_type, actor_id, actor_label, action, changes, detail) "
            "VALUES (:op, 'operator_user', :actor, :label, 'plan.update', 'null'::jsonb, 'null'::jsonb)"),
            {"op": op.id, "actor": sa.id, "label": sa.email})
        await db.commit()
    res = await env["client"].get("/admin/audit", headers=await headers_for(env, sa), params={"action": "plan.update"})
    [row] = res.json()["rows"]
    assert row["changes"] is None and row["detail"] is None


# ── Retention ────────────────────────────────────────────────────────────


async def test_nightly_purge_removes_only_rows_past_retention(env, monkeypatch):
    from src.config import get_settings
    from src.jobs import audit_retention

    op = await make_operator()
    now = datetime.now(timezone.utc)
    old = [(await seed(op.id, actor_type="system", action="voucher.expire",
                       created_at=audit_retention.months_before(now, 24) - timedelta(days=d))).id for d in (1, 2, 3, 40, 400)]
    kept = [(await seed(op.id, actor_type="system", action="voucher.expire",
                        created_at=audit_retention.months_before(now, 24) + timedelta(days=d))).id for d in (1, 30)]
    kept.append((await seed(op.id, actor_type="system", action="voucher.expire")).id)

    # Outside the window the table still refuses a delete.
    async with async_session_factory() as db:
        with pytest.raises(Exception, match="append-only"):
            await db.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": old[0]})
        await db.rollback()

    monkeypatch.setattr(get_settings(), "audit_purge_batch_size", 2)
    result = await audit_retention.purge_audit_log()
    assert result["deleted"] >= 5 and result["batches"] >= 3

    async with async_session_factory() as db:
        remaining = set((await db.execute(select(AuditLog.id).where(AuditLog.isp_operator_id == op.id))).scalars())
        purges = (await db.execute(select(AuditLog).where(AuditLog.action == "audit.purge")
                                   .order_by(AuditLog.created_at))).scalars().all()
    assert not remaining & set(old) and set(kept) <= remaining
    assert purges and sum(p.detail["deleted"] for p in purges) == result["deleted"]
    for p in purges:
        assert (p.actor_type, p.isp_operator_id, p.detail["retention_months"]) == ("system", None, 24)
    # Nothing left past retention: a second night deletes nothing and writes nothing.
    again = await audit_retention.purge_audit_log()
    assert again["deleted"] == 0
    async with async_session_factory() as db:
        assert len((await db.execute(select(AuditLog).where(AuditLog.action == "audit.purge"))).scalars().all()) == len(purges)


def test_months_before_clamps_the_day():
    from src.jobs.audit_retention import months_before

    assert months_before(datetime(2026, 3, 31, tzinfo=timezone.utc), 1) == datetime(2026, 2, 28, tzinfo=timezone.utc)
    assert months_before(datetime(2026, 10, 9, 3, 41, tzinfo=timezone.utc), 24) == datetime(2024, 10, 9, 3, 41, tzinfo=timezone.utc)
    assert months_before(datetime(2026, 1, 15, tzinfo=timezone.utc), 13) == datetime(2024, 12, 15, tzinfo=timezone.utc)
