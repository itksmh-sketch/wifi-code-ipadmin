"""Platform billing fixes against a REAL database (migration 064):

  * payments the platform owner records by hand — full amount only, audited,
    settle the invoice and lift a billing suspension; reversed (once, never a
    reversal) rather than edited; the ledger is append-only in the database;
  * another operator's invoice can't be recorded, reversed into, paid or read;
  * billing starts on the 1st after the trial: no invoice at trial end, no
    second invoice in a month, trial SMS usage on the first monthly invoice;
  * a GHS 0 operator is invoiced for SMS only once it reaches GHS 1;
  * the operator's billing API: line items, receipts, the real trial length;
  * the four trial templates the migration rewrote.

Runs the FastAPI app and the billing jobs in-process. Only runs against a
disposable database:

    BILLING_FIXES_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("BILLING_FIXES_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs BILLING_FIXES_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import func, select, text, update
    from sqlalchemy.exc import DBAPIError, IntegrityError

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import (
        AdminUser, AuditLog, ISPOperator, OperatorBillingEvent, OperatorInvoice, OperatorInvoiceLineItem,
        OperatorInvoicePayment, PlatformNotificationTemplate, PlatformOwner, SMSUsageRecord,
    )
    from src.jobs.invoice_generation import generate_monthly_invoices
    from src.jobs.trial_expiry import handle_trial_expiry
    from src.modules.auth.tokens import platform_owner_token_data
    from src.modules.billing import service as billing_service
    from src.modules.notifications import dispatcher
    from src.modules.notifications.template_catalog import DEFAULTS
    from src.modules.webhooks import platform_billing
    from src.utils.auth import create_platform_owner_access_token, hash_password

PASSWORD = "Str0ngPassword"
WEBHOOK_SECRET = "whsec-throwaway"
UTC = timezone.utc


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    async def no_limit(*args, **kwargs):
        return None

    for name, module in list(sys.modules.items()):
        if name.startswith("src.") and hasattr(module, "enforce_rate_limit"):
            monkeypatch.setattr(module, "enforce_rate_limit", no_limit)

    notices: list[dict] = []

    def capture(name):
        async def record(**kwargs):
            notices.append({"kind": name, **kwargs})
        return record

    for name in ("notify_grace_period", "notify_suspended", "notify_reactivated", "notify_invoice_issued",
                 "notify_trial_expired", "notify_trial_expiry_warning", "notify_operator_status_changed"):
        monkeypatch.setattr(dispatcher, name, capture(name))

    class _Keys:
        webhook_secret = WEBHOOK_SECRET
        secret_key = "sk_throwaway"
        public_key = "pk_throwaway"

    async def keys(db):
        return _Keys()

    monkeypatch.setattr(platform_billing, "resolve_paystack_keys", keys)

    async with async_session_factory() as db:
        owner = PlatformOwner(email=f"owner-{uuid.uuid4().hex[:8]}@throwaway.test", password_hash=hash_password(PASSWORD),
                              name="Owner", is_active=True, must_complete_security_setup=False)
        db.add(owner)
        await db.commit()
    token = create_platform_owner_access_token(platform_owner_token_data(owner))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as client:
        yield {"client": client, "owner": owner, "oh": {"Authorization": f"Bearer {token}"}, "notices": notices}


# ── builders ──────────────────────────────────────────────────────────────

async def make_operator(**values) -> "ISPOperator":
    slug = f"bf-{uuid.uuid4().hex[:10]}"
    state = {"status": "approved", "billing_status": "active", "monthly_fee_ghs": Decimal("50.00")}
    if values.get("status") == "suspended":
        state["suspension_reason"] = values.get("suspension_reason", "billing")
        state["billing_status"] = "past_due" if state["suspension_reason"] == "billing" else "paused"
    state.update(values)
    async with async_session_factory() as db:
        op = ISPOperator(name=f"BF {slug}", slug=slug, contact_email=f"{slug}@throwaway.test",
                         contact_phone="233244000111", **state)
        db.add(op)
        await db.commit()
        return op


async def make_invoice(op, *, amount="80.00", status="issued", days_late=0) -> "OperatorInvoice":
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        inv = OperatorInvoice(isp_operator_id=op.id, invoice_number=f"INV-BF-{uuid.uuid4().hex[:8]}",
                              period_start=now - timedelta(days=30), period_end=now, amount_ghs=Decimal(amount),
                              status=status, issued_at=now - timedelta(days=days_late + 7),
                              due_at=now - timedelta(days=days_late))
        db.add(inv)
        await db.commit()
        return inv


async def make_usage(op, amount, sent_at) -> "SMSUsageRecord":
    async with async_session_factory() as db:
        rec = SMSUsageRecord(isp_operator_id=op.id, segment_count=int(Decimal(amount) / Decimal("0.05")),
                             rate_ghs_per_segment=Decimal("0.05"), amount_ghs=Decimal(amount), sent_at=sent_at)
        db.add(rec)
        await db.commit()
        return rec


async def admin_headers(env, op) -> dict:
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        admin = AdminUser(isp_operator_id=op.id, email=f"a-{uuid.uuid4().hex[:10]}@throwaway.test",
                          password_hash=hash_password(PASSWORD), role="superadmin", is_active=True,
                          phone="233244000777", phone_verified=True, must_complete_onboarding=False,
                          must_change_password=False, pin_hash=hash_password("482913"), pin_set_at=now,
                          pin_verified_until=now + timedelta(minutes=10))
        db.add(admin)
        await db.commit()
    res = await env["client"].post("/auth/login", json={"email": admin.email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    async with async_session_factory() as db:  # sign-in ends PIN elevation; put it back
        await db.execute(update(AdminUser).where(AdminUser.id == admin.id)
                         .values(pin_verified_until=now + timedelta(minutes=10)))
        await db.commit()
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


async def fresh(model, id_):
    async with async_session_factory() as db:
        return await db.get(model, id_)


async def invoices_of(op):
    async with async_session_factory() as db:
        return (await db.execute(select(OperatorInvoice).where(OperatorInvoice.isp_operator_id == op.id)
                                 .order_by(OperatorInvoice.period_start))).scalars().all()


async def ledger(invoice):
    async with async_session_factory() as db:
        return (await db.execute(select(OperatorInvoicePayment).where(OperatorInvoicePayment.invoice_id == invoice.id)
                                 .order_by(OperatorInvoicePayment.created_at))).scalars().all()


async def audit_rows(action, target_id):
    async with async_session_factory() as db:
        return (await db.execute(select(AuditLog).where(AuditLog.action == action,
                                                        AuditLog.target_id == str(target_id))
                                 .order_by(AuditLog.created_at))).scalars().all()


def notices(env, kind, op):
    return [n for n in env["notices"] if n["kind"] == kind and n.get("isp_name") == op.name]


async def record(env, op, inv, **overrides):
    body = {"amount_ghs": str(inv.amount_ghs), "method": "momo", "reference": "MP-0001",
            "received_on": date.today().isoformat(), "note": "Paid to the platform MoMo number", **overrides}
    return await env["client"].post(f"/platform/operators/{op.id}/invoices/{inv.id}/payments",
                                    headers=env["oh"], json=body)


async def reverse(env, payment_id, reason="Recorded against the wrong invoice"):
    return await env["client"].post(f"/platform/invoice-payments/{payment_id}/reversal",
                                    headers=env["oh"], json={"reason": reason})


async def pay_by_webhook(env, invoice):
    raw = json.dumps({"event": "charge.success", "data": {
        "reference": f"INV-{invoice.id}", "status": "success", "amount": int(Decimal(invoice.amount_ghs) * 100),
        "currency": "GHS", "domain": "live", "metadata": {"invoice_id": str(invoice.id)}}}).encode()
    sig = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha512).hexdigest()
    return await env["client"].post("http://testserver/api/v1/webhooks/platform-billing/paystack", content=raw,
                                    headers={"x-paystack-signature": sig, "content-type": "application/json"})


# ── 7. payments recorded by hand ──────────────────────────────────────────

async def test_a_recorded_payment_settles_the_invoice_and_lifts_a_billing_suspension(env):
    op = await make_operator(status="suspended", suspension_reason="billing")
    inv = await make_invoice(op, status="overdue", days_late=30)
    received = date.today() - timedelta(days=2)
    env["notices"].clear()

    res = await record(env, op, inv, received_on=received.isoformat())
    assert res.status_code == 200, res.text
    assert res.json()["reactivated"] is True and res.json()["invoice_status"] == "paid"

    [entry] = await ledger(inv)
    assert (entry.kind, entry.method, entry.reference, entry.received_on, entry.amount_ghs) == \
        ("payment", "momo", "MP-0001", received, Decimal("80.00"))
    assert entry.recorded_by_platform_owner_id == env["owner"].id
    stored = await fresh(OperatorInvoice, inv.id)
    assert stored.status == "paid" and stored.payment_reference == f"manual:{entry.id}"
    assert stored.paid_at.date() == received  # counts in the month the money came in
    o = await fresh(ISPOperator, op.id)
    assert (o.status, o.billing_status, o.suspension_reason) == ("approved", "active", None)
    assert len(notices(env, "notify_reactivated", op)) == 1

    # Audited as the platform owner, with how the money came in.
    [paid] = await audit_rows("invoice.paid", inv.id)
    assert paid.actor_type == "platform_owner" and paid.actor_id == env["owner"].id
    assert paid.detail["method"] == "momo" and paid.detail["reference"] == "MP-0001"
    assert paid.detail["received_on"] == received.isoformat() and paid.detail["recorded_by_hand"] is True
    [reactivated] = await audit_rows("operator.reactivate", op.id)
    assert reactivated.actor_type == "platform_owner"
    async with async_session_factory() as db:
        [event] = (await db.execute(select(OperatorBillingEvent).where(
            OperatorBillingEvent.isp_operator_id == op.id, OperatorBillingEvent.event_type == "invoice_paid"))).scalars().all()
    assert event.event_metadata["method"] == "momo"


async def test_a_manual_suspension_stands_after_a_recorded_payment(env):
    op = await make_operator(status="suspended", suspension_reason="manual")
    inv = await make_invoice(op, status="overdue", days_late=30)
    env["notices"].clear()
    res = await record(env, op, inv, method="cash", reference=None)
    assert res.status_code == 200, res.text
    assert res.json()["reactivated"] is False
    assert (await fresh(OperatorInvoice, inv.id)).status == "paid"
    o = await fresh(ISPOperator, op.id)
    assert (o.status, o.billing_status, o.suspension_reason) == ("suspended", "paused", "manual")
    assert notices(env, "notify_reactivated", op) == []


@pytest.mark.parametrize("overrides,status,message", [
    ({"amount_ghs": "40.00"}, 400, "full amount"),                       # part payment
    ({"amount_ghs": "80.01"}, 400, "full amount"),                       # more than owed
    ({"method": "cheque"}, 400, "Method"),
    ({"method": "momo", "reference": "  "}, 400, "transaction reference"),
    ({"method": "bank", "reference": None}, 400, "transaction reference"),
    ({"received_on": (date.today() + timedelta(days=1)).isoformat()}, 400, "future"),
    ({"received_on": (date.today() - timedelta(days=400)).isoformat()}, 400, "more than a year"),
    ({"paid_by": "someone"}, 422, None),                                 # unknown field
])
async def test_a_payment_that_breaks_a_rule_is_refused_and_writes_nothing(env, overrides, status, message):
    op = await make_operator()
    inv = await make_invoice(op)
    res = await record(env, op, inv, **overrides)
    assert res.status_code == status, res.text
    if message:
        assert message in res.json()["detail"]
    assert await ledger(inv) == []
    assert (await fresh(OperatorInvoice, inv.id)).status == "issued"
    assert await audit_rows("invoice.paid", inv.id) == []


@pytest.mark.parametrize("invoice_status", ["paid", "waived"])
async def test_nothing_is_recorded_on_an_invoice_that_is_not_owed(env, invoice_status):
    op = await make_operator()
    inv = await make_invoice(op, status=invoice_status)
    res = await record(env, op, inv)
    assert res.status_code == 409, res.text
    assert await ledger(inv) == []


async def test_a_second_payment_on_a_paid_invoice_is_refused(env):
    op = await make_operator()
    inv = await make_invoice(op)
    assert (await record(env, op, inv)).status_code == 200
    res = await record(env, op, inv, reference="MP-0002")
    assert res.status_code == 409, res.text
    assert len(await ledger(inv)) == 1


async def test_another_operators_invoice_cannot_be_recorded_paid_or_read(env):
    mine, theirs = await make_operator(), await make_operator()
    their_invoice = await make_invoice(theirs)

    # The platform route: the operator in the path must own the invoice.
    res = await record(env, mine, their_invoice)
    assert res.status_code == 404, res.text
    assert await ledger(their_invoice) == []
    assert (await fresh(OperatorInvoice, their_invoice.id)).status == "issued"

    # The operator's own routes: someone else's invoice doesn't exist.
    h = await admin_headers(env, mine)
    c = env["client"]
    assert (await c.post(f"/billing/invoices/{their_invoice.id}/pay", headers=h)).status_code == 404
    assert (await c.get(f"/billing/invoices/{their_invoice.id}", headers=h)).status_code == 404
    listed = (await c.get("/billing/invoices", headers=h)).json()
    assert str(their_invoice.id) not in {row["id"] for row in listed}

    # And the database refuses an entry that names the wrong operator.
    async with async_session_factory() as db:
        db.add(OperatorInvoicePayment(isp_operator_id=mine.id, invoice_id=their_invoice.id, kind="payment",
                                      amount_ghs=Decimal("80.00"), method="cash", received_on=date.today(),
                                      recorded_by_platform_owner_id=env["owner"].id))
        with pytest.raises(DBAPIError, match="same operator"):
            await db.commit()


async def test_a_reversal_unpays_the_invoice_once_and_is_never_itself_reversed(env):
    op = await make_operator()
    inv = await make_invoice(op)
    payment_id = (await record(env, op, inv)).json()["payment_id"]

    assert (await reverse(env, payment_id, reason="  ")).status_code == 400   # a reason is required
    res = await reverse(env, payment_id)
    assert res.status_code == 200, res.text
    reversal_id = res.json()["reversal_id"]
    stored = await fresh(OperatorInvoice, inv.id)
    assert (stored.status, stored.paid_at, stored.payment_reference) == ("issued", None, None)

    [row] = await audit_rows("invoice.payment_reverse", inv.id)
    assert row.actor_type == "platform_owner" and row.changes == {"status": {"before": "paid", "after": "issued"}}
    assert row.detail["reason"] == "Recorded against the wrong invoice"
    async with async_session_factory() as db:
        events = (await db.execute(select(OperatorBillingEvent.event_type).where(
            OperatorBillingEvent.isp_operator_id == op.id))).scalars().all()
    assert "payment_reversed" in events

    again = await reverse(env, payment_id)
    assert again.status_code == 409 and "already been reversed" in again.json()["detail"]
    of_reversal = await reverse(env, reversal_id)
    assert of_reversal.status_code == 409 and "can't be reversed" in of_reversal.json()["detail"]
    assert (await reverse(env, uuid.uuid4())).status_code == 404

    # Recorded again, correctly: the history keeps all three entries.
    assert (await record(env, op, inv, method="bank", reference="GCB-778")).status_code == 200
    entries = await ledger(inv)
    assert [e.kind for e in entries] == ["payment", "reversal", "payment"]
    listed = (await env["client"].get("/platform/billing/invoices?page_size=200", headers=env["oh"])).json()
    [mine] = [i for i in listed["invoices"] if i["id"] == str(inv.id)]
    assert [p["reversed"] for p in mine["payments"]] == [True, False, False]
    assert mine["settled_by_payment_id"] == str(entries[2].id)


async def test_the_ledger_is_append_only_in_the_database(env):
    op = await make_operator()
    inv = await make_invoice(op)
    payment_id = uuid.UUID((await record(env, op, inv)).json()["payment_id"])
    reversal_id = uuid.UUID((await reverse(env, payment_id)).json()["reversal_id"])

    for statement in ("UPDATE operator_invoice_payments SET amount_ghs = 1 WHERE id = :id",
                      "DELETE FROM operator_invoice_payments WHERE id = :id"):
        async with async_session_factory() as db:
            with pytest.raises(DBAPIError, match="append-only"):
                await db.execute(text(statement), {"id": payment_id})

    def reversal(of):
        return OperatorInvoicePayment(isp_operator_id=op.id, invoice_id=inv.id, kind="reversal",
                                      amount_ghs=Decimal("80.00"), received_on=date.today(), note="x",
                                      reverses_payment_id=of, recorded_by_platform_owner_id=env["owner"].id)

    async with async_session_factory() as db:  # a reversal of a reversal
        db.add(reversal(reversal_id))
        with pytest.raises(DBAPIError, match="reverse one payment"):
            await db.commit()
    async with async_session_factory() as db:  # a second reversal of the same payment
        db.add(reversal(payment_id))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_a_paystack_payment_after_a_recorded_one_does_not_settle_twice(env):
    op = await make_operator()
    inv = await make_invoice(op)
    assert (await record(env, op, inv)).status_code == 200
    res = await pay_by_webhook(env, inv)
    assert res.status_code == 200 and res.json() == {"message": "Already paid"}
    assert len(await audit_rows("invoice.paid", inv.id)) == 1
    assert (await fresh(OperatorInvoice, inv.id)).payment_reference.startswith("manual:")


# ── 8-11. when invoices are issued ────────────────────────────────────────

async def test_trial_end_issues_no_invoice_and_billing_starts_on_the_first_of_next_month(env):
    trial_end = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    op = await make_operator(billing_status="trial", trial_ends_at=trial_end)
    env["notices"].clear()

    await handle_trial_expiry(now=trial_end + timedelta(hours=3))
    assert (await fresh(ISPOperator, op.id)).billing_status == "active"
    assert await invoices_of(op) == []
    [told] = notices(env, "notify_trial_expired", op)
    assert told["first_invoice_date"] == "November 01, 2026"
    [row] = await audit_rows("operator.trial_end", op.id)
    assert row.detail["first_invoice_date"] == "2026-11-01" and "invoiced" not in row.detail

    # The rest of October was trial: the daily run issues nothing.
    for day in (21, 31):
        await generate_monthly_invoices(now=datetime(2026, 10, day, 0, 0, tzinfo=UTC))
    assert await invoices_of(op) == []
    # 1 November: the first invoice. The runs after it add no second one.
    for day in (1, 2, 15):
        await generate_monthly_invoices(now=datetime(2026, 11, day, 0, 0, tzinfo=UTC))
    [first] = await invoices_of(op)
    assert str(first.period_start)[:10] == "2026-11-01" and first.amount_ghs == Decimal("50.00")
    await generate_monthly_invoices(now=datetime(2026, 12, 1, 0, 0, tzinfo=UTC))
    assert len(await invoices_of(op)) == 2


async def test_the_trial_warning_says_when_billing_starts(env):
    op = await make_operator(billing_status="trial", trial_ends_at=datetime(2026, 12, 30, 9, 0, tzinfo=UTC))
    env["notices"].clear()
    await handle_trial_expiry(now=datetime(2026, 12, 28, 6, 0, tzinfo=UTC))
    [warned] = notices(env, "notify_trial_expiry_warning", op)
    assert warned["first_invoice_date"] == "January 01, 2027"   # across the year end
    assert await invoices_of(op) == []


async def test_sms_used_during_the_trial_is_on_the_first_monthly_invoice(env):
    op = await make_operator(billing_status="trial", trial_ends_at=datetime(2026, 10, 20, 9, 0, tzinfo=UTC))
    await make_usage(op, "0.30", datetime(2026, 10, 10, tzinfo=UTC))   # during the trial
    await make_usage(op, "0.20", datetime(2026, 10, 25, tzinfo=UTC))   # after it, before November
    await handle_trial_expiry(now=datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
    await generate_monthly_invoices(now=datetime(2026, 11, 1, 0, 0, tzinfo=UTC))

    [inv] = await invoices_of(op)
    async with async_session_factory() as db:
        lines = (await db.execute(select(OperatorInvoiceLineItem).where(OperatorInvoiceLineItem.invoice_id == inv.id)
                                  .order_by(OperatorInvoiceLineItem.sort_order))).scalars().all()
        [event] = (await db.execute(select(OperatorBillingEvent).where(
            OperatorBillingEvent.isp_operator_id == op.id, OperatorBillingEvent.event_type == "invoice_issued"))).scalars().all()
    assert [(l.kind, l.amount_ghs) for l in lines] == [("subscription", Decimal("50.00")), ("sms_usage", Decimal("0.50"))]
    assert inv.amount_ghs == Decimal("50.50")
    # The event is written once every line is on: it states the real total.
    assert event.event_metadata["amount_ghs"] == "50.50"


async def test_a_zero_fee_operator_is_invoiced_for_sms_only_from_ghs_1(env):
    op = await make_operator(monthly_fee_ghs=Decimal("0.00"))
    early = await make_usage(op, "0.60", datetime(2026, 10, 12, tzinfo=UTC))

    await generate_monthly_invoices(now=datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
    assert await invoices_of(op) == []                                   # below GHS 1: carried forward
    assert (await fresh(SMSUsageRecord, early.id)).invoice_line_item_id is None

    await make_usage(op, "0.50", datetime(2026, 11, 9, tzinfo=UTC))
    await make_usage(op, "0.40", datetime(2026, 12, 3, tzinfo=UTC))      # December: next time
    for day in (1, 2):
        await generate_monthly_invoices(now=datetime(2026, 12, day, 0, 0, tzinfo=UTC))
    [inv] = await invoices_of(op)
    async with async_session_factory() as db:
        kinds = (await db.execute(select(OperatorInvoiceLineItem.kind).where(
            OperatorInvoiceLineItem.invoice_id == inv.id))).scalars().all()
        unbilled = (await db.execute(select(func.count()).select_from(SMSUsageRecord).where(
            SMSUsageRecord.isp_operator_id == op.id, SMSUsageRecord.invoice_line_item_id.is_(None)))).scalar()
    assert kinds == ["sms_usage"] and inv.amount_ghs == Decimal("1.10")
    assert unbilled == 1   # December's usage waits for its own month
    [issued] = [n for n in env["notices"] if n["kind"] == "notify_invoice_issued" and n["isp_name"] == op.name]
    assert issued["amount_ghs"] == Decimal("1.10")


# ── 12-14. what the operator sees ─────────────────────────────────────────

async def test_the_operator_sees_line_items_receipts_and_the_real_trial_length(env):
    now = datetime.now(UTC)
    op = await make_operator(billing_status="trial", trial_ends_at=now + timedelta(days=21))
    h = await admin_headers(env, op)
    c = env["client"]

    status = (await c.get("/billing/status", headers=h)).json()
    assert status["trial_length_days"] == 21 and status["trial_days_remaining"] in (20, 21)
    assert status["first_invoice_date"] == billing_service.first_invoice_date(now + timedelta(days=21)).isoformat()

    async with async_session_factory() as db:
        o = await db.get(ISPOperator, op.id)
        manual = await billing_service.create_invoice(db, o, date(2026, 11, 1), date(2026, 11, 30))
        online = await billing_service.create_invoice(db, o, date(2026, 12, 1), date(2026, 12, 31))
        await db.commit()

    assert (await record(env, op, manual, reference="MP-RCPT")).status_code == 200
    assert (await pay_by_webhook(env, online)).status_code == 200

    rows = {r["id"]: r for r in (await c.get("/billing/invoices", headers=h)).json()}
    assert [l["kind"] for l in rows[str(manual.id)]["line_items"]] == ["subscription"]
    assert Decimal(rows[str(manual.id)]["line_items"][0]["amount_ghs"]) == Decimal("50.00")

    detail = (await c.get(f"/billing/invoices/{manual.id}", headers=h)).json()
    assert detail["receipt"] == {"method": "Mobile money", "reference": "MP-RCPT",
                                 "paid_on": date.today().isoformat(), "amount_ghs": "50.00"}
    assert detail["bill_to"]["name"] == op.name and detail["bill_to"]["email"] == op.contact_email
    assert detail["issuer"]["name"]
    online_detail = (await c.get(f"/billing/invoices/{online.id}", headers=h)).json()
    assert online_detail["receipt"]["method"] == "Paystack (online)"
    assert online_detail["receipt"]["reference"] == f"INV-{online.id}"


async def test_an_unpaid_invoice_has_no_receipt(env):
    op = await make_operator()
    inv = await make_invoice(op)
    h = await admin_headers(env, op)
    detail = (await env["client"].get(f"/billing/invoices/{inv.id}", headers=h)).json()
    assert detail["receipt"] is None and detail["status"] == "issued"


async def test_the_migration_rewrote_the_trial_templates(env):
    async with async_session_factory() as db:
        rows = (await db.execute(select(PlatformNotificationTemplate).where(
            PlatformNotificationTemplate.event_type.in_(("trial_expired", "trial_expiry_warning"))))).scalars().all()
    assert len(rows) == 4
    for row in rows:
        d = DEFAULTS[(row.event_type, row.channel)]
        assert (row.subject, row.body_text, row.body_html) == (d.subject, d.body_text, d.body_html)
        assert "read-only" not in row.body_text
