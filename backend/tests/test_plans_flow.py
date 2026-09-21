"""Plan delete/deactivate and plan-edit rules against a REAL database.

Same guard as test_admin_accounts_flow: writes operators, plans, vouchers and
history rows, so it only runs when explicitly pointed at a disposable database
(ADMIN_FLOW_TEST_DATABASE_URL, whose name must contain "throwaway", equal to
DATABASE_URL) and skips everywhere else — including the production container.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("ADMIN_FLOW_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs ADMIN_FLOW_TEST_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:
    import httpx
    from sqlalchemy import select

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import (
        CoAEvent,
        CommissionRule,
        PaymentTransaction,
        Plan,
        Reseller,
        ResellerVoucherAllocation,
        Router,
        Session,
        Site,
        Town,
        Voucher,
    )
    from src.modules.admin_accounts import routes as account_routes
    from src.modules.platform import routes as platform_routes
    from src.modules.sms.types import SMSSendResult
    from src.utils.auth import hash_password

    from test_admin_accounts_flow import (  # the operator/admin plumbing is identical
        NEW_PASSWORD,
        OWNER_PASSWORD,
        SmsOutbox,
        bearer,
        create_operator,
        login,
        onboard,
    )

PLAN_BODY = {
    "name": "Flow Plan",
    "type": "time",
    "duration_minutes": 60,
    "download_speed_kbps": 1024,
    "upload_speed_kbps": 512,
    "price_ghs": 5,
}


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    """An onboarded operator admin, its operator id, and an SMS outbox."""
    outbox = SmsOutbox()
    monkeypatch.setattr(platform_routes, "send_temp_password_sms", outbox.temp_password)
    monkeypatch.setattr(account_routes, "send_otp_sms", outbox.otp)

    from src.db.models import PlatformOwner

    owner_email = f"owner-{uuid.uuid4().hex[:8]}@throwaway.test"
    async with async_session_factory() as db:
        db.add(PlatformOwner(email=owner_email, password_hash=hash_password(OWNER_PASSWORD), name="Owner", is_active=True))
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as client:
        res = await client.post("/platform/auth/login", json={"email": owner_email, "password": OWNER_PASSWORD})
        client.owner_headers = {"Authorization": f"Bearer {res.json()['access_token']}"}

        admin_email = f"plan-admin-{uuid.uuid4().hex[:6]}@throwaway.test"
        created = (await create_operator(client, admin_email=admin_email)).json()
        await onboard(client, outbox, admin_email, created["initial_admin"]["temp_password"],
                      phone=f"02440{uuid.uuid4().int % 100000:05d}")
        token = (await login(client, admin_email, NEW_PASSWORD)).json()["access_token"]
        client.admin_headers = bearer(token)
        client.operator_id = uuid.UUID(created["id"])
        yield client, outbox


async def make_plan(client, **overrides):
    body = {**PLAN_BODY, "name": f"Plan {uuid.uuid4().hex[:8]}", **overrides}
    res = await client.post("/plans", headers=client.admin_headers, json=body)
    assert res.status_code == 201, res.text
    return res.json()


async def add_voucher(operator_id, plan_id, **overrides):
    """A plain unused voucher straight in the database (generation goes through
    its own endpoint; these tests only care about what hangs off the row)."""
    async with async_session_factory() as db:
        voucher = Voucher(
            isp_operator_id=operator_id,
            plan_id=uuid.UUID(plan_id) if isinstance(plan_id, str) else plan_id,
            code=f"TEST-{uuid.uuid4().hex[:12].upper()}",
            username=f"U-{uuid.uuid4().hex[:12].upper()}",
            password="x",
            **overrides,
        )
        db.add(voucher)
        await db.commit()
        return voucher.id


async def site_and_router(operator_id):
    """A site + router to hang a session off. Reused across tests."""
    async with async_session_factory() as db:
        town = Town(isp_operator_id=operator_id, name=f"Town {uuid.uuid4().hex[:6]}", region="Greater Accra")
        db.add(town)
        await db.flush()
        site = Site(isp_operator_id=operator_id, town_id=town.id, name=f"Site {uuid.uuid4().hex[:6]}", address="1 Test Road")
        db.add(site)
        await db.flush()
        router = Router(
            isp_operator_id=operator_id, site_id=site.id, name=f"R-{uuid.uuid4().hex[:6]}",
            nas_identifier=f"nas-{uuid.uuid4().hex[:6]}", nas_secret="enc", is_active=True,
        )
        db.add(router)
        await db.commit()
        return site.id, router.id


async def delete_plan(client, plan_id):
    return await client.delete(f"/plans/{plan_id}", headers=client.admin_headers)


async def plan_exists(plan_id) -> bool:
    async with async_session_factory() as db:
        return (await db.execute(select(Plan.id).where(Plan.id == uuid.UUID(plan_id)))).first() is not None


# ── delete: the safe case ─────────────────────────────────────────────────


async def test_delete_removes_plan_and_its_untouched_vouchers(env):
    client, _ = env
    plan = await make_plan(client)
    for _ in range(3):
        await add_voucher(client.operator_id, plan["id"])

    res = await delete_plan(client, plan["id"])
    assert res.status_code == 204, res.text
    assert not await plan_exists(plan["id"])
    async with async_session_factory() as db:
        left = (await db.execute(select(Voucher.id).where(Voucher.plan_id == uuid.UUID(plan["id"])))).all()
    assert left == []


async def test_delete_also_removes_this_plans_commission_rules(env):
    client, _ = env
    plan = await make_plan(client)
    async with async_session_factory() as db:
        reseller = Reseller(
            isp_operator_id=client.operator_id, name="R", email=f"r-{uuid.uuid4().hex[:8]}@throwaway.test",
            password_hash="x", role="reseller",
        )
        db.add(reseller)
        await db.flush()
        db.add(CommissionRule(isp_operator_id=client.operator_id, reseller_id=reseller.id,
                              plan_id=uuid.UUID(plan["id"]), type="percentage", value=Decimal("10")))
        # A rule that is not tied to this plan must survive.
        db.add(CommissionRule(isp_operator_id=client.operator_id, reseller_id=reseller.id,
                              plan_id=None, type="percentage", value=Decimal("5")))
        await db.commit()

    assert (await delete_plan(client, plan["id"])).status_code == 204
    async with async_session_factory() as db:
        remaining = (await db.execute(select(CommissionRule).where(CommissionRule.isp_operator_id == client.operator_id))).scalars().all()
    assert [r.plan_id for r in remaining] == [None]


async def test_delete_of_a_plan_with_no_vouchers_at_all(env):
    client, _ = env
    plan = await make_plan(client)
    assert (await delete_plan(client, plan["id"])).status_code == 204
    assert (await delete_plan(client, plan["id"])).status_code == 404


# ── delete: every history type blocks ─────────────────────────────────────


async def test_payment_on_a_voucher_blocks_delete(env):
    client, _ = env
    plan = await make_plan(client)
    voucher_id = await add_voucher(client.operator_id, plan["id"])
    site_id, _router_id = await site_and_router(client.operator_id)
    async with async_session_factory() as db:
        db.add(PaymentTransaction(
            isp_operator_id=client.operator_id, plan_id=uuid.UUID(plan["id"]), site_id=site_id,
            voucher_id=voucher_id, amount_ghs=Decimal("5.00"), payment_method="mtn_momo",
            provider="paystack", internal_reference=f"ref-{uuid.uuid4().hex[:10]}",
        ))
        await db.commit()

    res = await delete_plan(client, plan["id"])
    assert res.status_code == 409
    assert "payment" in res.json()["detail"]
    assert await plan_exists(plan["id"])
    async with async_session_factory() as db:  # nothing was written
        assert (await db.execute(select(Voucher.id).where(Voucher.id == voucher_id))).first() is not None


async def test_reseller_allocation_blocks_delete(env):
    client, _ = env
    plan = await make_plan(client)
    voucher_id = await add_voucher(client.operator_id, plan["id"])
    async with async_session_factory() as db:
        reseller = Reseller(
            isp_operator_id=client.operator_id, name="R", email=f"r-{uuid.uuid4().hex[:8]}@throwaway.test",
            password_hash="x", role="reseller",
        )
        db.add(reseller)
        await db.flush()
        db.add(ResellerVoucherAllocation(reseller_id=reseller.id, voucher_id=voucher_id,
                                         purchase_price_ghs=Decimal("4.00")))
        await db.commit()

    res = await delete_plan(client, plan["id"])
    assert res.status_code == 409 and "reseller" in res.json()["detail"]
    assert await plan_exists(plan["id"])


async def test_session_blocks_delete(env):
    client, _ = env
    plan = await make_plan(client)
    voucher_id = await add_voucher(client.operator_id, plan["id"])
    _site_id, router_id = await site_and_router(client.operator_id)
    async with async_session_factory() as db:
        db.add(Session(
            isp_operator_id=client.operator_id, voucher_id=voucher_id, router_id=router_id,
            username="U", nas_ip="10.100.0.9", session_id=f"S-{uuid.uuid4().hex[:8]}",
            started_at=datetime.now(timezone.utc),
        ))
        await db.commit()

    res = await delete_plan(client, plan["id"])
    assert res.status_code == 409 and "used" in res.json()["detail"]
    assert await plan_exists(plan["id"])


async def test_coa_event_blocks_delete(env):
    client, _ = env
    plan = await make_plan(client)
    voucher_id = await add_voucher(client.operator_id, plan["id"])
    async with async_session_factory() as db:
        db.add(CoAEvent(isp_operator_id=client.operator_id, voucher_id=voucher_id, event_type="disconnect"))
        await db.commit()

    res = await delete_plan(client, plan["id"])
    assert res.status_code == 409 and "disconnect" in res.json()["detail"]
    assert await plan_exists(plan["id"])


@pytest.mark.parametrize("overrides", [
    {"status": "active"},
    {"status": "disabled"},
    {"status": "exhausted"},
    {"activated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
    {"data_used_mb": 5},
])
async def test_used_voucher_blocks_delete_even_without_a_session_row(env, overrides):
    """The Acct-Session-Id collision could leave a used voucher with no session."""
    client, _ = env
    plan = await make_plan(client)
    await add_voucher(client.operator_id, plan["id"], **overrides)
    res = await delete_plan(client, plan["id"])
    assert res.status_code == 409 and "activated, disabled or used up" in res.json()["detail"]
    assert await plan_exists(plan["id"])


async def test_payment_referencing_the_plan_without_a_voucher_blocks_delete(env):
    client, _ = env
    plan = await make_plan(client)
    site_id, _router_id = await site_and_router(client.operator_id)
    async with async_session_factory() as db:
        db.add(PaymentTransaction(
            isp_operator_id=client.operator_id, plan_id=uuid.UUID(plan["id"]), site_id=site_id,
            voucher_id=None, amount_ghs=Decimal("5.00"), payment_method="mtn_momo",
            provider="paystack", internal_reference=f"ref-{uuid.uuid4().hex[:10]}",
        ))
        await db.commit()

    res = await delete_plan(client, plan["id"])
    assert res.status_code == 409 and "payment(s) reference this plan" in res.json()["detail"]
    assert await plan_exists(plan["id"])


async def test_history_message_lists_every_reason(env):
    client, _ = env
    plan = await make_plan(client)
    voucher_id = await add_voucher(client.operator_id, plan["id"], status="active")
    _site_id, router_id = await site_and_router(client.operator_id)
    async with async_session_factory() as db:
        db.add(Session(
            isp_operator_id=client.operator_id, voucher_id=voucher_id, router_id=router_id,
            username="U", nas_ip="10.100.0.9", session_id=f"S-{uuid.uuid4().hex[:8]}",
            started_at=datetime.now(timezone.utc),
        ))
        db.add(CoAEvent(isp_operator_id=client.operator_id, voucher_id=voucher_id, event_type="disconnect"))
        await db.commit()

    detail = (await delete_plan(client, plan["id"])).json()["detail"]
    assert "used" in detail and "disconnect" in detail and "Deactivate it instead" in detail


# ── deactivate / activate ─────────────────────────────────────────────────


async def test_deactivate_and_activate(env):
    client, _ = env
    plan = await make_plan(client)
    voucher_id = await add_voucher(client.operator_id, plan["id"], status="active")

    res = await client.post(f"/plans/{plan['id']}/deactivate", headers=client.admin_headers)
    assert res.status_code == 200 and res.json()["is_active"] is False
    res = await client.post(f"/plans/{plan['id']}/activate", headers=client.admin_headers)
    assert res.status_code == 200 and res.json()["is_active"] is True

    # Deactivating never touches the vouchers already issued.
    async with async_session_factory() as db:
        voucher = (await db.execute(select(Voucher).where(Voucher.id == voucher_id))).scalar_one()
    assert voucher.status == "active"

    assert (await client.post(f"/plans/{uuid.uuid4()}/deactivate", headers=client.admin_headers)).status_code == 404


async def test_plan_endpoints_are_tenant_scoped(env):
    client, outbox = env
    plan = await make_plan(client)
    # A second operator's admin must not see or delete this plan.
    other_email = f"other-{uuid.uuid4().hex[:6]}@throwaway.test"
    other = (await create_operator(client, admin_email=other_email)).json()
    await onboard(client, outbox, other_email, other["initial_admin"]["temp_password"],
                  phone=f"02441{uuid.uuid4().int % 100000:05d}")
    other_token = (await login(client, other_email, NEW_PASSWORD)).json()["access_token"]

    assert (await client.delete(f"/plans/{plan['id']}", headers=bearer(other_token))).status_code == 404
    assert (await client.post(f"/plans/{plan['id']}/deactivate", headers=bearer(other_token))).status_code == 404
    assert await plan_exists(plan["id"])


# ── PATCH /plans/{id}: allowlist ──────────────────────────────────────────


async def test_patch_edits_name_price_and_speeds(env):
    client, _ = env
    plan = await make_plan(client, price_ghs=5, download_speed_kbps=1024, upload_speed_kbps=512)
    res = await client.patch(f"/plans/{plan['id']}", headers=client.admin_headers,
                             json={"name": "  Renamed Plan  ", "price_ghs": 7.5,
                                   "download_speed_kbps": 2048, "upload_speed_kbps": 1024})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["name"] == "Renamed Plan"
    assert float(body["price_ghs"]) == 7.5
    assert (body["download_speed_kbps"], body["upload_speed_kbps"]) == (2048, 1024)
    # Entitlement untouched.
    assert (body["type"], body["duration_minutes"], body["data_limit_mb"]) == (
        plan["type"], plan["duration_minutes"], plan["data_limit_mb"])


@pytest.mark.parametrize("payload,named", [
    ({"type": "data"}, "type"),
    ({"duration_minutes": 120}, "duration_minutes"),
    ({"data_limit_mb": 500}, "data_limit_mb"),
    # Mixed with a legitimate field: the whole call is refused.
    ({"name": "New", "type": "data"}, "type"),
    ({"price_ghs": 9, "duration_minutes": 30}, "duration_minutes"),
    ({"name": "New", "duration_minutes": 30, "data_limit_mb": 10}, "data_limit_mb"),
    # Neighbours that are not part of this endpoint either.
    ({"is_active": False}, "is_active"),
    ({"site_id": str(uuid.uuid4())}, "site_id"),
    ({"isp_operator_id": str(uuid.uuid4())}, "isp_operator_id"),
    ({"id": str(uuid.uuid4())}, "id"),
])
async def test_patch_refuses_protected_fields_by_name(env, payload, named):
    client, _ = env
    plan = await make_plan(client)
    res = await client.patch(f"/plans/{plan['id']}", headers=client.admin_headers, json=payload)
    assert res.status_code == 400, res.text
    assert named in res.json()["detail"]
    after = (await client.get("/plans", headers=client.admin_headers)).json()
    stored = next(p for p in after if p["id"] == plan["id"])
    assert stored == plan  # nothing changed at all


@pytest.mark.parametrize("payload", [
    {},                                  # nothing to update
    {"name": ""},                        # blank name
    {"name": "   "},
    {"price_ghs": -1},                   # negative price
    {"download_speed_kbps": 0},          # speeds must be positive
    {"upload_speed_kbps": -5},
    {"name": "x" * 256},
])
async def test_patch_validation_errors_are_400(env, payload):
    client, _ = env
    plan = await make_plan(client)
    res = await client.patch(f"/plans/{plan['id']}", headers=client.admin_headers, json=payload)
    assert res.status_code == 400, (payload, res.text)


async def test_patch_refuses_an_edit_that_duplicates_another_plan(env):
    client, _ = env
    first = await make_plan(client, price_ghs=5)
    second = await make_plan(client, price_ghs=9)
    res = await client.patch(f"/plans/{second['id']}", headers=client.admin_headers, json={"price_ghs": 5})
    assert res.status_code == 409
    assert first["name"] in res.json()["detail"]


async def test_patch_is_tenant_scoped_and_404s_for_unknown_plans(env):
    client, outbox = env
    plan = await make_plan(client)
    other_email = f"pother-{uuid.uuid4().hex[:6]}@throwaway.test"
    other = (await create_operator(client, admin_email=other_email)).json()
    await onboard(client, outbox, other_email, other["initial_admin"]["temp_password"],
                  phone=f"02442{uuid.uuid4().int % 100000:05d}")
    other_token = (await login(client, other_email, NEW_PASSWORD)).json()["access_token"]

    assert (await client.patch(f"/plans/{plan['id']}", headers=bearer(other_token),
                               json={"name": "Hijack"})).status_code == 404
    assert (await client.patch(f"/plans/{uuid.uuid4()}", headers=client.admin_headers,
                               json={"name": "Ghost"})).status_code == 404


@pytest.mark.parametrize("method", ["patch", "put"])
@pytest.mark.parametrize("payload,named", [
    ({"type": "data"}, "type"),
    ({"duration_minutes": 120}, "duration_minutes"),
    ({"data_limit_mb": 500}, "data_limit_mb"),
    ({"is_active": False}, "is_active"),
])
async def test_put_enforces_the_same_allowlist_as_patch(env, method, payload, named):
    """PUT used to accept every column, which let an edit rewrite entitlement."""
    client, _ = env
    plan = await make_plan(client)
    res = await getattr(client, method)(f"/plans/{plan['id']}", headers=client.admin_headers, json=payload)
    assert res.status_code == 400 and named in res.json()["detail"]
    after = (await client.get("/plans", headers=client.admin_headers)).json()
    assert next(p for p in after if p["id"] == plan["id"]) == plan


@pytest.mark.parametrize("method", ["patch", "put"])
async def test_both_editors_accept_the_allowed_fields(env, method):
    client, _ = env
    plan = await make_plan(client)
    res = await getattr(client, method)(f"/plans/{plan['id']}", headers=client.admin_headers,
                                        json={"name": f"Edited {method}", "price_ghs": 6.25})
    assert res.status_code == 200, res.text
    assert res.json()["name"] == f"Edited {method}" and float(res.json()["price_ghs"]) == 6.25


# ── operator purchase-SMS template (Phase C) ──────────────────────────────


async def test_voucher_sms_template_defaults_then_saves_and_resets(env):
    client, _ = env
    res = await client.get("/sms-template/voucher", headers=client.admin_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["is_default"] is True and body["template"] is None
    assert body["preview"]["segment_count"] == 1
    assert {p["key"] for p in body["placeholders"]} == {"code", "plan", "validity", "operator"}
    assert "default 16-character" in body["code_length_caveat"]

    custom = "{operator}: your code is {code} ({plan}, {validity}). Enjoy!"
    res = await client.put("/sms-template/voucher", headers=client.admin_headers, json={"template": custom})
    assert res.status_code == 200, res.text
    assert res.json()["template"] == custom and res.json()["is_default"] is False

    # Reset.
    res = await client.put("/sms-template/voucher", headers=client.admin_headers, json={"template": None})
    assert res.status_code == 200 and res.json()["is_default"] is True


@pytest.mark.parametrize("template,fragment", [
    ("Plan {plan} only", "must include {code}"),
    ("Code {code} expires {expiry}", "Unknown placeholder"),
    ("", "cannot be empty"),
    ("{code}" + "x" * 200, "over the 160-character"),
])
async def test_voucher_sms_template_refuses_bad_input(env, template, fragment):
    client, _ = env
    res = await client.put("/sms-template/voucher", headers=client.admin_headers, json={"template": template})
    assert res.status_code == 400, res.text
    assert fragment in res.json()["detail"]
    # Nothing stored.
    assert (await client.get("/sms-template/voucher", headers=client.admin_headers)).json()["is_default"] is True


async def test_voucher_sms_template_preview_does_not_save(env):
    client, _ = env
    res = await client.post("/sms-template/voucher/preview", headers=client.admin_headers,
                            json={"template": "{code} " + "x" * 200})
    assert res.status_code == 200
    body = res.json()
    assert body["valid"] is False and "160-character" in body["error"]
    assert body["preview"]["segment_count"] > 1
    assert (await client.get("/sms-template/voucher", headers=client.admin_headers)).json()["is_default"] is True

    res = await client.post("/sms-template/voucher/preview", headers=client.admin_headers,
                            json={"template": "Code {code}"})
    assert res.json()["valid"] is True and res.json()["preview"]["segment_count"] == 1


async def test_voucher_sms_template_rejects_unknown_fields_and_needs_auth(env):
    client, _ = env
    assert (await client.put("/sms-template/voucher", headers=client.admin_headers,
                             json={"template": "Code {code}", "operator_id": "x"})).status_code == 422
    assert (await client.get("/sms-template/voucher")).status_code == 403


# ── fields the operator SMS editor reads ──────────────────────────────────
# VoucherSmsTemplate.jsx is a hand-written fetch caller: a renamed field renders
# as "undefined" rather than failing, so each one it reads is asserted here.

async def test_voucher_template_payload_carries_every_field_the_editor_reads(env):
    client, _ = env
    body = (await client.get("/sms-template/voucher", headers=client.admin_headers)).json()
    # applyPayload() reads these three.
    for field in ("effective_template", "is_default", "preview"):
        assert field in body, f"missing {field}"
    # The placeholder chips render key and use description as the tooltip.
    assert body["placeholders"]
    for placeholder in body["placeholders"]:
        assert set(placeholder) >= {"key", "description"}
    # The Stat tiles read all four of these.
    for field in ("text", "encoding", "segment_count", "character_count"):
        assert field in body["preview"], f"preview missing {field}"
    assert body["preview"]["encoding"] in ("gsm7", "ucs2")
    # Shown verbatim under the counters.
    assert body["code_length_caveat"]


async def test_voucher_template_preview_returns_200_with_its_reason_when_invalid(env):
    """The editor shows preview.error inline; a non-200 would surface as a
    generic failure instead of naming the bad placeholder."""
    client, _ = env
    res = await client.post(
        "/sms-template/voucher/preview", headers=client.admin_headers, json={"template": "No code here."}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["valid"] is False
    assert "{code}" in body["error"]
    assert set(body["preview"]) >= {"text", "encoding", "segment_count", "character_count"}


async def test_a_refused_voucher_template_save_puts_its_reason_in_detail(env):
    """The save handler reads err.message, which ApiError builds from detail."""
    client, _ = env
    res = await client.put(
        "/sms-template/voucher", headers=client.admin_headers, json={"template": "Hi {nope}, your code."}
    )
    assert res.status_code == 400
    assert "{nope}" in res.json()["detail"]


# ── fields the plan Edit dialog reads ─────────────────────────────────────
# Plans.jsx opens the dialog from the row object it already has, so GET /plans
# must carry the read-only entitlement fields too — not just the editable ones.

async def test_plan_list_carries_every_field_the_edit_dialog_shows(env):
    client, _ = env
    await make_plan(client)
    rows = (await client.get("/plans", headers=client.admin_headers)).json()
    assert rows
    for row in rows:
        # Editable inputs are seeded from these.
        for field in ("id", "name", "price_ghs", "download_speed_kbps", "upload_speed_kbps"):
            assert field in row, f"missing {field}"
        # Shown read-only under "What this plan grants".
        for field in ("type", "duration_minutes", "data_limit_mb"):
            assert field in row, f"missing {field}"


async def test_patching_a_single_changed_field_leaves_the_others_alone(env):
    """editChanges() sends only what the operator actually touched."""
    client, _ = env
    plan = await make_plan(client, price_ghs=5, download_speed_kbps=1024, upload_speed_kbps=512)

    res = await client.patch(f"/plans/{plan['id']}", headers=client.admin_headers, json={"name": "Renamed Only"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["name"] == "Renamed Only"
    assert float(body["price_ghs"]) == float(plan["price_ghs"])
    assert body["download_speed_kbps"] == plan["download_speed_kbps"]
    assert body["upload_speed_kbps"] == plan["upload_speed_kbps"]

    res = await client.patch(f"/plans/{plan['id']}", headers=client.admin_headers, json={"price_ghs": 6.25})
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "Renamed Only"
    assert float(res.json()["price_ghs"]) == 6.25


async def test_the_dialogs_refusals_all_carry_a_readable_detail(env):
    """Both branches show e.message inline, which ApiError builds from detail —
    a refusal with no detail would surface as a generic failure."""
    client, _ = env
    first = await make_plan(client, price_ghs=5)
    second = await make_plan(client, price_ghs=9)

    conflict = await client.patch(f"/plans/{second['id']}", headers=client.admin_headers, json={"price_ghs": 5})
    assert conflict.status_code == 409
    assert conflict.json().get("detail")

    invalid = await client.patch(f"/plans/{second['id']}", headers=client.admin_headers, json={"name": "   "})
    assert invalid.status_code == 400
    assert invalid.json().get("detail")


async def test_an_unchanged_save_would_be_refused_so_the_dialog_must_not_send_it(env):
    """saveEdit() closes without a request when nothing changed. This pins the
    reason: an empty PATCH is a 400, not a no-op."""
    client, _ = env
    plan = await make_plan(client)
    res = await client.patch(f"/plans/{plan['id']}", headers=client.admin_headers, json={})
    assert res.status_code == 400
    assert "Nothing to update" in res.json()["detail"]
