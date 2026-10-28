"""Operator status (approve / suspend / unsuspend / cancel / restore) against a
REAL database: what each status allows, the transition table, the platform
route's side effects, the billing fixes (B1-B3), notices and audit rows.

Runs the FastAPI app in-process. Only runs against a disposable database at
migration 058 or later:

    OPERATOR_STATUS_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("OPERATOR_STATUS_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs OPERATOR_STATUS_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import (
        AdminOtpCode, AdminUser, AuditLog, ISPOperator, OperatorBillingEvent, OperatorInvoice,
        PlatformNotificationTemplate,
        PlatformOwner, Reseller, ResellerWallet, Router, Site, Town,
    )
    from src.middleware import auth as auth_middleware
    from src.modules.admin_accounts import routes as account_routes
    from src.modules.auth import routes as auth_routes
    from src.modules.auth.tokens import platform_owner_token_data
    from src.modules.notifications import dispatcher
    from src.modules.notifications.template_catalog import DEFAULTS
    from src.modules.platform import routes as platform_routes
    from src.modules.resellers import routes as reseller_routes
    from src.modules.webhooks import platform_billing
    from src.portal import routes as portal_routes
    from src.utils.auth import create_platform_owner_access_token, hash_password
    from src.utils.portal_token import create_portal_router_token
    from src.utils.reseller_auth import hash_reseller_password

PASSWORD = "Str0ngPassword"
PHONE = "233244000777"
WEBHOOK_SECRET = "whsec-throwaway"
REASON = "Chargeback ring under review XYZZY"
STATUSES = ("pending", "approved", "suspended", "cancelled")
ALLOWED = {
    "pending": {"approved", "cancelled"},
    "approved": {"suspended", "cancelled"},
    "suspended": {"approved", "cancelled"},
    "cancelled": {"approved"},
}


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    async def no_limit(*args, **kwargs):
        return None

    for module in (account_routes, auth_routes, auth_middleware, reseller_routes, platform_routes, portal_routes):
        monkeypatch.setattr(module, "enforce_rate_limit", no_limit, raising=False)

    notices: list[dict] = []

    async def capture_status_notice(**kwargs):
        notices.append({"kind": "status", **kwargs})

    async def capture_access_notice(**kwargs):
        notices.append({"kind": "access", **kwargs})

    async def capture_billing_notice(name):
        async def capture(**kwargs):
            notices.append({"kind": name, **kwargs})
        return capture

    real_status_notice = dispatcher.notify_operator_status_changed
    monkeypatch.setattr(dispatcher, "notify_operator_status_changed", capture_status_notice)
    monkeypatch.setattr(dispatcher, "notify_admin_access_changed", capture_access_notice)
    for name in ("notify_grace_period", "notify_suspended", "notify_reactivated", "notify_invoice_issued",
                 "notify_trial_expired", "notify_trial_expiry_warning"):
        monkeypatch.setattr(dispatcher, name, await capture_billing_notice(name))

    async def no_temp_sms(*args, **kwargs):
        from src.modules.sms.types import SMSSendResult
        return SMSSendResult(success=True, provider_reference="test")

    monkeypatch.setattr(platform_routes, "send_temp_password_sms", no_temp_sms)
    from src.modules.admin_accounts import notifications as admin_notifications
    monkeypatch.setattr(admin_notifications, "send_temp_password_sms", no_temp_sms)

    class _Keys:
        webhook_secret = WEBHOOK_SECRET
        secret_key = "sk_throwaway"
        public_key = "pk_throwaway"

    async def keys(db):
        return _Keys()

    monkeypatch.setattr(platform_billing, "resolve_paystack_keys", keys)

    async with async_session_factory() as db:
        owner = PlatformOwner(
            email=f"owner-{uuid.uuid4().hex[:8]}@throwaway.test", password_hash=hash_password(PASSWORD),
            name="Owner", is_active=True, must_complete_security_setup=False,
        )
        db.add(owner)
        await db.commit()
    owner_token = create_platform_owner_access_token(platform_owner_token_data(owner))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as client:
        yield {"client": client, "owner": owner, "owner_headers": bearer(owner_token), "notices": notices,
               "real_status_notice": real_status_notice}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _now():
    return datetime.now(timezone.utc)


def _state(status: str, *, billing_status: str | None = None, suspension_reason: str | None = None) -> dict:
    """Column values that satisfy migration 058's CHECKs for ``status``."""
    values = {"status": status, "billing_status": billing_status or "active", "suspension_reason": None,
              "cancelled_at": None, "hotspot_service_ends_at": None}
    if status == "suspended":
        values["suspension_reason"] = suspension_reason or "manual"
        values["billing_status"] = billing_status or ("paused" if values["suspension_reason"] == "manual" else "past_due")
    if status == "cancelled":
        now = _now()
        values.update(billing_status="cancelled", cancelled_at=now, hotspot_service_ends_at=now + timedelta(days=30))
    return values


async def make_operator(status="approved", **overrides) -> "ISPOperator":
    slug = f"ost-{uuid.uuid4().hex[:10]}"
    values = _state(status, billing_status=overrides.pop("billing_status", None),
                    suspension_reason=overrides.pop("suspension_reason", None))
    values.update(overrides)
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Ost {slug}", slug=slug, contact_email=f"{slug}@throwaway.test",
                               contact_phone="233244000111", monthly_fee_ghs=Decimal("50.00"), **values)
        db.add(operator)
        await db.commit()
        return operator


async def make_admin(operator, *, role="superadmin") -> "AdminUser":
    async with async_session_factory() as db:
        admin = AdminUser(
            isp_operator_id=operator.id, email=f"adm-{uuid.uuid4().hex[:10]}@throwaway.test",
            password_hash=hash_password(PASSWORD), role=role, is_active=True, phone=PHONE, phone_verified=True,
            must_complete_onboarding=False, must_change_password=False, security_question="birth_town",
            security_answer_hash=hash_password("accra"),
        )
        db.add(admin)
        await db.commit()
        return admin


async def make_reseller(operator, *, balance="0.00") -> "Reseller":
    async with async_session_factory() as db:
        reseller = Reseller(isp_operator_id=operator.id, name="Res", email=f"res-{uuid.uuid4().hex[:10]}@throwaway.test",
                            password_hash=hash_reseller_password(PASSWORD), role="reseller", is_active=True)
        db.add(reseller)
        await db.flush()
        db.add(ResellerWallet(reseller_id=reseller.id, balance_ghs=Decimal(balance)))
        await db.commit()
        return reseller


async def portal_token_for(operator) -> str:
    async with async_session_factory() as db:
        town = Town(isp_operator_id=operator.id, name=f"Town {uuid.uuid4().hex[:6]}", region="Greater Accra")
        db.add(town)
        await db.flush()
        site = Site(isp_operator_id=operator.id, town_id=town.id, name=f"Site {uuid.uuid4().hex[:6]}", address="1 Test Road")
        db.add(site)
        await db.flush()
        router = Router(isp_operator_id=operator.id, site_id=site.id, name=f"R-{uuid.uuid4().hex[:6]}",
                        nas_identifier=f"nas-{uuid.uuid4().hex[:6]}", nas_secret="enc", is_active=True)
        db.add(router)
        await db.commit()
        return create_portal_router_token(str(router.id))


async def make_invoice(operator, *, days_late: int, status="issued", amount="50.00") -> "OperatorInvoice":
    now = _now()
    async with async_session_factory() as db:
        invoice = OperatorInvoice(isp_operator_id=operator.id, invoice_number=f"INV-OST-{uuid.uuid4().hex[:8]}",
                                  period_start=now - timedelta(days=60), period_end=now - timedelta(days=30),
                                  amount_ghs=Decimal(amount), status=status, issued_at=now - timedelta(days=60),
                                  due_at=now - timedelta(days=days_late))
        db.add(invoice)
        await db.commit()
        return invoice


async def fresh(model, row_id):
    async with async_session_factory() as db:
        return await db.get(model, row_id)


async def set_status(env, operator, status, **body):
    return await env["client"].put(f"/platform/operators/{operator.id}/status", headers=env["owner_headers"],
                                   json={"status": status, **body})


async def sign_in(client, admin):
    return await client.post("/auth/login", json={"email": admin.email, "password": PASSWORD})


async def reseller_sign_in(client, reseller):
    return await client.post("/reseller/auth/login", json={"email": reseller.email, "password": PASSWORD})


async def unknown_admin_reply(client):
    return await client.post("/auth/login", json={"email": f"ghost-{uuid.uuid4().hex}@throwaway.test", "password": PASSWORD})


async def unknown_reseller_reply(client):
    return await client.post("/reseller/auth/login", json={"email": f"ghost-{uuid.uuid4().hex}@throwaway.test",
                                                           "password": PASSWORD})


async def status_rows(operator):
    async with async_session_factory() as db:
        return (await db.execute(
            select(AuditLog).where(AuditLog.target_id == str(operator.id), AuditLog.action == "operator.status_change")
            .order_by(AuditLog.created_at)
        )).scalars().all()


def notices_for(env, operator):
    return [n for n in env["notices"] if n.get("isp_name") == operator.name]


async def portal_buy(client, token):
    return await client.post("http://testserver/portal/initiate-payment",
                             json={"plan_id": str(uuid.uuid4()), "rt": token, "phone": "0244000999",
                                   "payment_method": "mtn_momo"})


# ── 1. what each status allows (the report's table) ────────────────────────


@pytest.mark.parametrize("status,logins_off", [(s, off) for s in STATUSES for off in (False, True)])
async def test_status_matrix(env, status, logins_off):
    client = env["client"]
    operator = await make_operator("approved")
    admin = await make_admin(operator)
    reseller = await make_reseller(operator)
    token = await portal_token_for(operator)
    # Sessions from before the change.
    admin_tokens = (await sign_in(client, admin)).json()
    reseller_tokens = (await reseller_sign_in(client, reseller)).json()

    async with async_session_factory() as db:
        op = await db.get(ISPOperator, operator.id)
        for key, value in _state(status).items():
            setattr(op, key, value)
        if logins_off:
            op.admin_logins_disabled_at = _now()
        await db.commit()

    signs_in = status != "cancelled" and not logins_off
    sells = status not in ("suspended", "cancelled")

    login = await sign_in(client, admin)
    assert (login.status_code == 200) is signs_in, login.text
    if not signs_in:
        ghost = await unknown_admin_reply(client)
        assert (login.status_code, login.json()) == (ghost.status_code, ghost.json())
    me = await client.get("/auth/onboarding/status", headers=bearer(admin_tokens["access_token"]))
    assert (me.status_code == 200) is signs_in
    refreshed = await client.post("/auth/refresh", json={"refresh_token": admin_tokens["refresh_token"]})
    assert (refreshed.status_code == 200) is signs_in

    r_login = await reseller_sign_in(client, reseller)
    assert (r_login.status_code == 200) is signs_in, r_login.text
    if not signs_in:
        ghost = await unknown_reseller_reply(client)
        assert (r_login.status_code, r_login.json()) == (ghost.status_code, ghost.json())
    r_me = await client.get("/reseller/me", headers=bearer(reseller_tokens["access_token"]))
    assert (r_me.status_code == 200) is signs_in

    # A plan id that doesn't exist: an open operator gets 404, a blocked one 503
    # before the plan is even looked up.
    bought = await portal_buy(client, token)
    assert bought.status_code == (404 if sells else 503), bought.text

    if signs_in:
        # Reseller stock comes from the operator now: allocating is what a
        # suspension must block (the plan id doesn't exist, so an open
        # operator gets 404).
        fresh_admin = (await sign_in(client, admin)).json()
        allocated = await client.post(f"/admin/resellers/{reseller.id}/allocations",
                                      headers=bearer(fresh_admin["access_token"]),
                                      json={"plan_id": str(uuid.uuid4()), "quantity": 1})
        assert allocated.status_code == (404 if sells else 403), allocated.text


# ── 2. the transition table ───────────────────────────────────────────────


@pytest.mark.parametrize("before,after", [(b, a) for b in STATUSES for a in STATUSES])
async def test_transition_table(env, before, after):
    operator = await make_operator(before)
    res = await set_status(env, operator, after, reason="table check")
    if after == before:
        assert res.status_code == 409, res.text
    elif after in ALLOWED[before]:
        assert res.status_code == 200, res.text
        assert res.json()["status"] == after
    else:
        assert res.status_code == 400, res.text
    if res.status_code != 200:
        # A refused move changes nothing, writes no row and sends nothing.
        assert (await fresh(ISPOperator, operator.id)).status == before
        assert await status_rows(operator) == []
        assert notices_for(env, operator) == []


async def test_unknown_status_and_bad_bodies(env):
    operator = await make_operator("approved")
    assert (await set_status(env, operator, "frozen")).status_code == 400
    assert (await set_status(env, operator, "suspended", hotspot_service_days=5)).status_code == 400
    assert (await set_status(env, operator, "cancelled", hotspot_service_days=366)).status_code == 422
    assert (await set_status(env, operator, "cancelled", hotspot_service_days=-1)).status_code == 422
    assert (await set_status(env, operator, "suspended", reason="x" * 501)).status_code == 422
    assert (await set_status(env, operator, "suspended", extra_field=1)).status_code == 422
    assert (await fresh(ISPOperator, operator.id)).status == "approved"


async def test_unknown_operator_404(env):
    res = await env["client"].put(f"/platform/operators/{uuid.uuid4()}/status", headers=env["owner_headers"],
                                  json={"status": "suspended"})
    assert res.status_code == 404


async def test_status_change_waits_for_the_row_lock_and_sees_the_new_state(env):
    """The route locks the operator row: a change made under a lock it waited
    for is what it decides on, not the state it would have read before."""
    operator = await make_operator("approved")
    async with async_session_factory() as db:
        held = (await db.execute(select(ISPOperator).where(ISPOperator.id == operator.id).with_for_update())).scalar_one()
        request = asyncio.ensure_future(set_status(env, operator, "suspended"))
        await asyncio.sleep(0.5)
        assert not request.done()  # waiting on our lock
        for key, value in _state("cancelled").items():
            setattr(held, key, value)
        await db.commit()
    res = await request
    assert res.status_code == 400, res.text  # cancelled -> suspended is not a move
    assert (await fresh(ISPOperator, operator.id)).status == "cancelled"
    assert await status_rows(operator) == []


# ── 3. what each move does ────────────────────────────────────────────────


async def test_manual_suspend_pauses_invoicing_keeps_sign_in(env):
    client = env["client"]
    operator = await make_operator("approved", billing_status="active")
    admin = await make_admin(operator)
    tokens = (await sign_in(client, admin)).json()

    res = await set_status(env, operator, "suspended", reason=REASON)
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["status"], body["billing_status"], body["suspension_reason"]) == ("suspended", "paused", "manual")

    # Sign-in stays open; capacity-creating writes are refused.
    assert (await client.get("/auth/onboarding/status", headers=bearer(tokens["access_token"]))).status_code == 200
    assert (await sign_in(client, admin)).status_code == 200

    [row] = await status_rows(operator)
    assert row.changes == {"status": {"before": "approved", "after": "suspended"},
                           "billing_status": {"before": "active", "after": "paused"},
                           "suspension_reason": {"before": None, "after": "manual"}}
    assert row.detail == {"reason": REASON}
    assert row.actor_type == "platform_owner"

    [notice] = notices_for(env, operator)
    assert notice == {"kind": "status", "event": "account_suspended_manual", "email": operator.contact_email,
                      "phone": operator.contact_phone, "isp_name": operator.name}


async def test_paused_operator_is_not_invoiced(env):
    from src.jobs.invoice_generation import generate_monthly_invoices

    paused = await make_operator("approved", billing_status="active")
    assert (await set_status(env, paused, "suspended")).status_code == 200
    active = await make_operator("approved", billing_status="active")
    await generate_monthly_invoices()
    async with async_session_factory() as db:
        def count(op):
            return select(OperatorInvoice).where(OperatorInvoice.isp_operator_id == op.id)
        assert (await db.execute(count(paused))).scalars().all() == []
        assert len((await db.execute(count(active))).scalars().all()) == 1


async def test_suspending_a_trial_operator_and_unsuspending_resumes_trial(env):
    operator = await make_operator("approved", billing_status="trial", trial_ends_at=_now() + timedelta(days=5))
    assert (await set_status(env, operator, "suspended")).json()["billing_status"] == "paused"
    res = await set_status(env, operator, "approved")
    assert (res.json()["status"], res.json()["billing_status"], res.json()["suspension_reason"]) == ("approved", "trial", None)


async def test_unsuspend_after_trial_ended_goes_active_and_notifies(env):
    operator = await make_operator("suspended", trial_ends_at=_now() - timedelta(days=1))
    res = await set_status(env, operator, "approved", reason="Cleared")
    assert res.status_code == 200
    assert (res.json()["billing_status"], res.json()["suspension_reason"]) == ("active", None)
    [notice] = notices_for(env, operator)
    assert notice["event"] == "account_restored"
    [row] = await status_rows(operator)
    assert row.changes["billing_status"] == {"before": "paused", "after": "active"}


async def test_unsuspending_a_billing_suspension_leaves_billing_past_due(env):
    operator = await make_operator("suspended", suspension_reason="billing")
    res = await set_status(env, operator, "approved")
    assert (res.json()["status"], res.json()["billing_status"], res.json()["suspension_reason"]) == ("approved", "past_due", None)


async def test_cancel_signs_everyone_out_and_keeps_them_out(env):
    client = env["client"]
    operator = await make_operator("approved")
    admin = await make_admin(operator)
    other = await make_admin(operator, role="admin")
    reseller = await make_reseller(operator)
    tokens = (await sign_in(client, admin)).json()
    other_tokens = (await sign_in(client, other)).json()
    r_tokens = (await reseller_sign_in(client, reseller)).json()
    async with async_session_factory() as db:
        db.add(AdminOtpCode(admin_user_id=admin.id, purpose="reset", phone=PHONE, code_hash="x",
                            expires_at=_now() + timedelta(minutes=10)))
        await db.commit()

    res = await set_status(env, operator, "cancelled", reason=REASON)
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["status"], body["billing_status"], body["suspension_reason"]) == ("cancelled", "cancelled", None)

    for t in (tokens, other_tokens):
        assert (await client.get("/auth/onboarding/status", headers=bearer(t["access_token"]))).status_code == 401
        assert (await client.post("/auth/refresh", json={"refresh_token": t["refresh_token"]})).status_code == 401
    assert (await client.get("/reseller/me", headers=bearer(r_tokens["access_token"]))).status_code == 401
    assert (await sign_in(client, admin)).status_code == 401
    assert (await reseller_sign_in(client, reseller)).status_code == 401

    async with async_session_factory() as db:
        open_codes = (await db.execute(select(AdminOtpCode).where(
            AdminOtpCode.admin_user_id == admin.id, AdminOtpCode.consumed_at.is_(None)))).scalars().all()
        assert open_codes == []
        refreshed = await db.get(AdminUser, admin.id)
        assert refreshed.pin_verified_until is None

    op = await fresh(ISPOperator, operator.id)
    assert op.hotspot_service_ends_at - op.cancelled_at == timedelta(days=30)
    assert op.admin_logins_disabled_at is None  # the P2 switch is a separate axis

    [row] = await status_rows(operator)
    assert row.detail == {"reason": REASON, "hotspot_service_days": 30, "admins_signed_out": 2, "resellers_blocked": 1}
    assert set(row.changes) == {"status", "billing_status", "cancelled_at", "hotspot_service_ends_at"}
    [notice] = notices_for(env, operator)
    assert notice["event"] == "account_cancelled"


@pytest.mark.parametrize("days", [0, 7, 365])
async def test_cancel_wind_down_days(env, days):
    operator = await make_operator("approved")
    assert (await set_status(env, operator, "cancelled", hotspot_service_days=days)).status_code == 200
    op = await fresh(ISPOperator, operator.id)
    assert op.hotspot_service_ends_at - op.cancelled_at == timedelta(days=days)


async def test_cancel_from_a_billing_suspension_clears_the_reason(env):
    operator = await make_operator("suspended", suspension_reason="billing")
    res = await set_status(env, operator, "cancelled")
    assert (res.json()["status"], res.json()["billing_status"], res.json()["suspension_reason"]) == ("cancelled", "cancelled", None)


async def test_restore_reopens_sign_in_without_reviving_old_sessions(env):
    client = env["client"]
    operator = await make_operator("approved", trial_ends_at=_now() - timedelta(days=40))
    admin = await make_admin(operator)
    reseller = await make_reseller(operator)
    old = (await sign_in(client, admin)).json()
    assert (await set_status(env, operator, "cancelled")).status_code == 200

    res = await set_status(env, operator, "approved", reason="Paid up in cash")
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["status"], body["billing_status"], body["cancelled_at"], body["hotspot_service_ends_at"]) == \
        ("approved", "active", None, None)

    assert (await client.get("/auth/onboarding/status", headers=bearer(old["access_token"]))).status_code == 401
    assert (await client.post("/auth/refresh", json={"refresh_token": old["refresh_token"]})).status_code == 401
    assert (await sign_in(client, admin)).status_code == 200
    assert (await reseller_sign_in(client, reseller)).status_code == 200
    assert [n["event"] for n in notices_for(env, operator)] == ["account_cancelled", "account_restored"]


async def test_restore_to_trial_and_keeps_the_sign_in_switch(env):
    client = env["client"]
    operator = await make_operator("cancelled", trial_ends_at=_now() + timedelta(days=3),
                                   admin_logins_disabled_at=_now())
    admin = await make_admin(operator)
    res = await set_status(env, operator, "approved")
    assert res.json()["billing_status"] == "trial"
    assert res.json()["admin_logins_disabled_at"] is not None
    assert (await sign_in(client, admin)).status_code == 401  # still off by the switch


async def test_approve_pending_sends_nothing(env):
    operator = await make_operator("pending")
    res = await set_status(env, operator, "approved")
    assert res.status_code == 200
    op = await fresh(ISPOperator, operator.id)
    assert op.approved_at is not None and op.approved_by_platform_owner_id == env["owner"].id
    assert notices_for(env, operator) == []


async def test_check_constraints_hold(env):
    operator = await make_operator("approved")
    async with async_session_factory() as db:
        op = await db.get(ISPOperator, operator.id)
        op.status = "cancelled"  # without cancelled_at
        with pytest.raises(IntegrityError):
            await db.commit()
    async with async_session_factory() as db:
        op = await db.get(ISPOperator, operator.id)
        op.billing_status = "paused"  # while approved
        with pytest.raises(IntegrityError):
            await db.commit()


# ── 4. platform-side actions on a cancelled operator ──────────────────────


async def test_platform_refuses_sign_in_changes_while_cancelled(env):
    client, h = env["client"], env["owner_headers"]
    operator = await make_operator("approved")
    admin = await make_admin(operator)
    assert (await client.post(f"/platform/operators/{operator.id}/admin-logins/disable", headers=h,
                              json={"reason": "fraud"})).status_code == 200
    assert (await client.post(f"/platform/operators/{operator.id}/admins/{admin.id}/disable", headers=h,
                              json={"reason": "fraud", "confirm_last_superadmin": True})).status_code == 200
    assert (await set_status(env, operator, "cancelled")).status_code == 200
    env["notices"].clear()

    for path, body in (
        (f"/platform/operators/{operator.id}/admin-logins/enable", {}),
        (f"/platform/operators/{operator.id}/admins/{admin.id}/enable", {}),
        (f"/platform/operators/{operator.id}/admins/{admin.id}/reset-password", {}),
        (f"/platform/operators/{operator.id}/admins", {"email": f"new-{uuid.uuid4().hex[:6]}@throwaway.test",
                                                      "phone": "0244000556", "role": "admin"}),
    ):
        res = await client.post(path, headers=h, json=body)
        assert res.status_code == 409, (path, res.text)
        assert res.json()["detail"] == platform_routes.OPERATOR_CANCELLED
    assert env["notices"] == []


async def test_operator_detail_shows_what_is_owed(env):
    operator = await make_operator("approved")
    await make_reseller(operator, balance="12.50")
    await make_reseller(operator, balance="7.50")
    unpaid = await make_invoice(operator, days_late=3, status="overdue")
    await make_invoice(operator, days_late=40, status="paid")
    res = await env["client"].get(f"/platform/operators/{operator.id}", headers=env["owner_headers"])
    assert res.status_code == 200
    body = res.json()
    assert [i["invoice_number"] for i in body["unpaid_invoices"]] == [unpaid.invoice_number]
    assert body["reseller_wallet_total_ghs"] == 20.0


# ── 5. billing fixes ──────────────────────────────────────────────────────


async def test_b1_enforcement_suspends_only_approved_operators(env):
    from src.config import get_settings
    from src.jobs.billing_enforcement import enforce_billing

    late = get_settings().grace_period_days + 2
    cancelled = await make_operator("cancelled")
    pending = await make_operator("pending")
    manual = await make_operator("suspended")
    control = await make_operator("approved")
    issued_for_cancelled = await make_invoice(cancelled, days_late=late)
    for op in (pending, manual, control):
        await make_invoice(op, days_late=late)
    env["notices"].clear()

    await enforce_billing()
    # Step 1 marks issued invoices overdue on one run, step 2 suspends on the next.
    await enforce_billing()

    assert (await fresh(ISPOperator, cancelled.id)).status == "cancelled"
    assert (await fresh(ISPOperator, cancelled.id)).billing_status == "cancelled"
    assert (await fresh(ISPOperator, pending.id)).status == "pending"
    m = await fresh(ISPOperator, manual.id)
    assert (m.status, m.billing_status, m.suspension_reason) == ("suspended", "paused", "manual")
    c = await fresh(ISPOperator, control.id)
    assert (c.status, c.billing_status, c.suspension_reason) == ("suspended", "past_due", "billing")
    # The cancelled operator's invoice is still marked overdue, but nobody is
    # told to pay "to avoid suspension".
    assert (await fresh(OperatorInvoice, issued_for_cancelled.id)).status == "overdue"
    names = {n["isp_name"] for n in env["notices"] if n["kind"] == "notify_grace_period"}
    assert cancelled.name not in names and control.name in names
    suspended_names = {n["isp_name"] for n in env["notices"] if n["kind"] == "notify_suspended"}
    assert suspended_names & {cancelled.name, pending.name, manual.name} == set()
    assert control.name in suspended_names


def _charge(invoice, domain="live") -> bytes:
    pesewas = int(Decimal(invoice.amount_ghs) * 100)
    data = {"reference": f"INV-{invoice.id}", "status": "success", "amount": pesewas, "currency": "GHS",
            "metadata": {"invoice_id": str(invoice.id)}}
    if domain is not None:  # None: the field is left out
        data["domain"] = domain
    return json.dumps({"event": "charge.success", "data": data}).encode()


async def pay_by_webhook(client, invoice, domain="live"):
    raw = _charge(invoice, domain)
    sig = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha512).hexdigest()
    return await client.post("http://testserver/api/v1/webhooks/platform-billing/paystack", content=raw,
                             headers={"x-paystack-signature": sig, "content-type": "application/json"})


@pytest.mark.parametrize("status,billing,reason", [
    ("cancelled", "cancelled", None),
    ("suspended", "paused", "manual"),
])
async def test_b2_payment_never_revives_a_cancelled_or_paused_operator(env, status, billing, reason):
    operator = await make_operator(status, suspension_reason=reason)
    invoice = await make_invoice(operator, days_late=40, status="overdue")
    env["notices"].clear()
    res = await pay_by_webhook(env["client"], invoice)
    assert res.status_code == 200, res.text
    assert (await fresh(OperatorInvoice, invoice.id)).status == "paid"
    op = await fresh(ISPOperator, operator.id)
    assert (op.status, op.billing_status, op.suspension_reason) == (status, billing, reason)
    assert [n for n in env["notices"] if n["kind"] == "notify_reactivated"] == []


async def test_b2_control_billing_suspension_is_still_lifted_by_payment(env):
    operator = await make_operator("suspended", suspension_reason="billing")
    invoice = await make_invoice(operator, days_late=40, status="overdue")
    env["notices"].clear()
    assert (await pay_by_webhook(env["client"], invoice)).status_code == 200
    op = await fresh(ISPOperator, operator.id)
    assert (op.status, op.billing_status, op.suspension_reason) == ("approved", "active", None)
    assert [n["isp_name"] for n in env["notices"] if n["kind"] == "notify_reactivated"] == [operator.name]


@pytest.mark.parametrize("domain", ["test", None])
async def test_b4_a_test_mode_charge_never_pays_an_invoice(env, domain):
    """Signed with the platform key (which today is a test key), right amount,
    right currency, right reference — but made in Paystack test mode, or with
    no mode at all. It is refused and recorded, and nothing about the invoice
    or the operator moves."""
    operator = await make_operator("suspended", suspension_reason="billing")
    invoice = await make_invoice(operator, days_late=40, status="overdue")
    env["notices"].clear()

    res = await pay_by_webhook(env["client"], invoice, domain=domain)
    # 200, not an error: the same payload would fail the same way on every retry.
    assert res.status_code == 200, res.text
    assert res.json() == {"message": "Payment rejected: not_live"}

    inv = await fresh(OperatorInvoice, invoice.id)
    assert (inv.status, inv.paid_at, inv.payment_reference) == ("overdue", None, None)
    op = await fresh(ISPOperator, operator.id)
    assert (op.status, op.billing_status, op.suspension_reason) == ("suspended", "past_due", "billing")
    assert env["notices"] == []

    async with async_session_factory() as db:
        events = (await db.execute(
            select(OperatorBillingEvent).where(OperatorBillingEvent.isp_operator_id == operator.id)
        )).scalars().all()
        paid_rows = (await db.execute(
            select(AuditLog).where(AuditLog.target_id == str(invoice.id), AuditLog.action == "invoice.paid")
        )).scalars().all()
    assert [e.event_type for e in events] == ["payment_rejected"]
    meta = events[0].event_metadata
    assert meta["rejection"] == "not_live"
    assert meta["domain"] == (domain or "")
    assert meta["invoice_id"] == str(invoice.id)
    assert meta["payment_reference"] == f"INV-{invoice.id}"
    assert paid_rows == []

    # The refusal leaves the invoice payable: the live charge that follows settles it.
    assert (await pay_by_webhook(env["client"], invoice)).status_code == 200
    assert (await fresh(OperatorInvoice, invoice.id)).status == "paid"
    op = await fresh(ISPOperator, operator.id)
    assert (op.status, op.billing_status, op.suspension_reason) == ("approved", "active", None)


async def test_b4_billing_event_types_match_the_database(env):
    # A value the database has but the model lacks is written fine and then
    # raises LookupError on every load (payment_rejected did, until this test).
    from sqlalchemy import text
    async with async_session_factory() as db:
        in_db = (await db.execute(
            text("SELECT unnest(enum_range(NULL::operator_billing_event_type))::text")
        )).scalars().all()
    assert set(OperatorBillingEvent.__table__.c.event_type.type.enums) == set(in_db)


async def test_b4_a_test_mode_replay_after_payment_changes_nothing(env):
    operator = await make_operator("approved")
    invoice = await make_invoice(operator, days_late=0)
    assert (await pay_by_webhook(env["client"], invoice)).status_code == 200
    paid = await fresh(OperatorInvoice, invoice.id)
    res = await pay_by_webhook(env["client"], invoice, domain="test")
    assert res.json() == {"message": "Already paid"}
    again = await fresh(OperatorInvoice, invoice.id)
    assert (again.status, again.paid_at, again.payment_reference) == ("paid", paid.paid_at, paid.payment_reference)


# ── 6. notices and the audit view ─────────────────────────────────────────


async def test_058_seeded_the_six_template_rows(env):
    async with async_session_factory() as db:
        rows = (await db.execute(select(PlatformNotificationTemplate).where(
            PlatformNotificationTemplate.event_type.in_(dispatcher.OPERATOR_STATUS_EVENTS)))).scalars().all()
    assert {(r.event_type, r.channel) for r in rows} == {
        (e, c) for e in dispatcher.OPERATOR_STATUS_EVENTS for c in ("email", "sms")}
    for r in rows:
        assert r.body_text == DEFAULTS[(r.event_type, r.channel)].body_text


@pytest.mark.parametrize("event", ["account_suspended_manual", "account_restored", "account_cancelled"])
async def test_real_notice_renders_sms_and_email_without_any_reason(env, monkeypatch, event):
    sent: list[tuple] = []

    async def sms(to, message):
        sent.append(("sms", to, message))

    async def email(to, subject, html, text):
        sent.append(("email", to, subject, html, text))

    monkeypatch.setattr(dispatcher, "_send_sms", sms)
    monkeypatch.setattr(dispatcher, "_send_email", email)
    await env["real_status_notice"](event=event, email="ops@throwaway.test", phone="233244000111", isp_name="Aflao Net")
    assert [s[0] for s in sent] == ["email", "sms"]
    message = sent[1][2]
    assert "Aflao Net" in message and "{" not in message
    # Either address may be missing: that channel is skipped.
    sent.clear()
    await env["real_status_notice"](event=event, email=None, phone="233244000111", isp_name="Aflao Net")
    assert [s[0] for s in sent] == ["sms"]
    with pytest.raises(ValueError):
        await env["real_status_notice"](event="account_suspended", email=None, phone=None, isp_name="x")


async def test_operator_audit_view_hides_the_reason_platform_view_shows_it(env):
    client = env["client"]
    operator = await make_operator("approved")
    admin = await make_admin(operator)
    assert (await set_status(env, operator, "suspended", reason=REASON)).status_code == 200
    tokens = (await sign_in(client, admin)).json()

    own = await client.get("/admin/audit", headers=bearer(tokens["access_token"]),
                           params={"action": "operator.status_change"})
    assert own.status_code == 200, own.text
    [row] = own.json()["rows"]
    assert row["actor"]["label"] == "Platform"
    assert REASON not in json.dumps(row)

    platform = await client.get("/platform/audit", headers=env["owner_headers"],
                                params={"action": "operator.status_change", "operator": str(operator.id)})
    assert platform.status_code == 200, platform.text
    mine = [r for r in platform.json()["rows"] if r["target_id"] == str(operator.id)]
    assert mine and mine[0]["detail"]["reason"] == REASON


async def test_status_notice_never_carries_the_reason(env):
    operator = await make_operator("approved")
    for status in ("suspended", "approved", "cancelled", "approved"):
        assert (await set_status(env, operator, status, reason=REASON)).status_code == 200
    sent = notices_for(env, operator)
    assert [n["event"] for n in sent] == ["account_suspended_manual", "account_restored",
                                          "account_cancelled", "account_restored"]
    for n in sent:
        assert set(n) == {"kind", "event", "email", "phone", "isp_name"}
        assert REASON not in json.dumps(n)
    assert len(await status_rows(operator)) == 4
