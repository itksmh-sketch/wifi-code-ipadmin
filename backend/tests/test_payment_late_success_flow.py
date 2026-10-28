"""Payments the reconciliation job timed out, confirmed later, against a REAL
database: the job keeps asking the provider for 48 hours (at most every 30
minutes); a success issues the voucher, queues the SMS once and tells the
operator; a provider failure ends the re-checks; no verdict changes nothing.
A webhook or a forced re-check can settle one too, at any age. A payment the
provider itself failed is never revived.

Only runs against a disposable database:

    PAYMENT_LATE_SUCCESS_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>

(tests/throwaway_db.py run sets both.)
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("PAYMENT_LATE_SUCCESS_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs PAYMENT_LATE_SUCCESS_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    from sqlalchemy import select

    from src.db.base import async_session_factory
    from src.db.models import AuditLog, ISPOperator, PaymentTransaction, Plan, Site, Town, Voucher
    from src.jobs.payment_reconciliation import run_payment_reconciliation
    from src.modules.payments import operator_alerts
    from src.modules.payments import service as payment_service_module
    from src.modules.payments.providers.base import provider_unreachable_result
    from src.modules.payments.service import PaymentService
    from src.modules.payments.types import PaymentStatus, PaymentVerificationResult
    from src.modules.webhooks.processor import process_webhook_event

PHONE = "233244000555"


def verdict(status: str, reason: str | None = None):
    return PaymentVerificationResult(status=PaymentStatus(status), amount_ghs=Decimal("2.00"), failure_reason=reason,
                                     provider_state=status)


class FakeProvider:
    def __init__(self, env):
        self.env = env

    async def verify(self, provider_reference, expected_amount_ghs=None):
        self.env["asked"].append(provider_reference)
        answer = self.env["verdicts"].get(provider_reference, verdict("pending"))
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    state = {"asked": [], "verdicts": {}, "sms": [], "late": [], "contradicted": []}

    async def provider_for_transaction(self, db, tx):
        return FakeProvider(state)

    async def enqueue(payment_id):
        state["sms"].append(payment_id)
        return True

    async def late(payment_id):
        state["late"].append(payment_id)

    async def contradicted(payment_id):
        state["contradicted"].append(payment_id)

    monkeypatch.setattr(PaymentService, "provider_for_transaction", provider_for_transaction)
    monkeypatch.setattr(payment_service_module, "enqueue_voucher_sms", enqueue)
    monkeypatch.setattr(operator_alerts, "notify_late_success", late)
    monkeypatch.setattr(operator_alerts, "notify_success_after_failure", contradicted)
    yield state


async def make_tenant() -> dict:
    slug = f"late-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Late {slug}", slug=slug, contact_email=f"{slug}@throwaway.test", status="approved")
        db.add(operator)
        await db.flush()
        town = Town(isp_operator_id=operator.id, name="Town", region="Greater Accra")
        db.add(town)
        await db.flush()
        site = Site(isp_operator_id=operator.id, town_id=town.id, name="Site", address="1 Test Road")
        plan = Plan(isp_operator_id=operator.id, name="1h", type="time", duration_minutes=60,
                    download_speed_kbps=1024, upload_speed_kbps=512, price_ghs=Decimal("2.00"))
        db.add_all([site, plan])
        await db.commit()
    return {"operator": operator, "site": site, "plan": plan}


async def make_failed(t, *, age: timedelta, reason: str = "timeout", checked_ago: timedelta | None = None,
                      reference: bool = True, phone: str | None = PHONE) -> tuple[uuid.UUID, str, str]:
    now = datetime.now(timezone.utc)
    provider_reference = f"PSK-{uuid.uuid4().hex[:12]}" if reference else None
    async with async_session_factory() as db:
        tx = PaymentTransaction(
            isp_operator_id=t["operator"].id, plan_id=t["plan"].id, site_id=t["site"].id, amount_ghs=Decimal("2.00"),
            payment_method="mtn_momo", provider="paystack", status="failed", failure_reason=reason,
            provider_reference=provider_reference, internal_reference=f"INT-{uuid.uuid4().hex[:12]}",
            phone_number=phone, initiated_at=now - age, completed_at=now - age + timedelta(hours=2),
            last_status_check_at=(now - checked_ago) if checked_ago is not None else None,
        )
        db.add(tx)
        await db.commit()
        return tx.id, tx.internal_reference, provider_reference


async def load(tx_id) -> PaymentTransaction:
    async with async_session_factory() as db:
        return await db.get(PaymentTransaction, tx_id)


async def vouchers_for(reference: str) -> int:
    async with async_session_factory() as db:
        rows = (await db.execute(select(Voucher).where(Voucher.batch_id == f"PAY-{reference[:8]}"))).scalars().all()
        return len(rows)


async def status_rows(tx_id) -> list:
    async with async_session_factory() as db:
        return (await db.execute(select(AuditLog).where(AuditLog.action == "payment.status_change",
                                                        AuditLog.target_id == str(tx_id)))).scalars().all()


async def webhook(t, reference: str, status: str = "success", provider: str = "paystack"):
    await process_webhook_event(None, provider, reference, status, None, "{}", isp_operator_id=str(t["operator"].id))


# ── the 48-hour re-check ──────────────────────────────────────────────────

async def test_late_success_issues_the_voucher_texts_it_and_tells_the_operator(env):
    t = await make_tenant()
    tx_id, ref, psk = await make_failed(t, age=timedelta(hours=5), checked_ago=timedelta(hours=3))
    env["verdicts"][psk] = verdict("success")

    stats = await run_payment_reconciliation(isp_operator_id=str(t["operator"].id))

    tx = await load(tx_id)
    assert tx.status == "success" and tx.voucher_id is not None and tx.failure_reason is None
    assert await vouchers_for(ref) == 1
    assert env["sms"].count(tx_id) == 1
    assert env["late"] == [tx_id]
    [row] = await status_rows(tx_id)
    assert row.changes == {"status": {"before": "failed", "after": "success"}}
    assert row.detail["late"] is True and "timed out" in row.summary
    assert stats["late_checked"] == 1 and stats["late_success"] == 1

    # A second run finds nothing left to do: no second voucher, SMS or alert.
    env["sms"].clear()
    env["late"].clear()
    stats = await run_payment_reconciliation(isp_operator_id=str(t["operator"].id))
    assert stats["late_checked"] == 0 and await vouchers_for(ref) == 1
    assert env["sms"] == [] and env["late"] == []


async def test_a_payment_older_than_48_hours_is_not_checked_again(env):
    t = await make_tenant()
    _, _, inside = await make_failed(t, age=timedelta(hours=47), checked_ago=timedelta(hours=1))
    tx_id, _, outside = await make_failed(t, age=timedelta(hours=49), checked_ago=timedelta(hours=1))
    env["asked"].clear()
    await run_payment_reconciliation(isp_operator_id=str(t["operator"].id))
    assert inside in env["asked"] and outside not in env["asked"]
    assert (await load(tx_id)).status == "failed"


async def test_checks_are_at_least_30_minutes_apart(env):
    t = await make_tenant()
    _, _, recent = await make_failed(t, age=timedelta(hours=3), checked_ago=timedelta(minutes=10))
    _, _, due = await make_failed(t, age=timedelta(hours=3), checked_ago=timedelta(minutes=31))
    _, _, never = await make_failed(t, age=timedelta(hours=3), checked_ago=None)
    env["asked"].clear()
    await run_payment_reconciliation(isp_operator_id=str(t["operator"].id))
    assert recent not in env["asked"]
    assert due in env["asked"] and never in env["asked"]


async def test_a_provider_failure_records_its_reason_and_ends_the_checks(env):
    t = await make_tenant()
    tx_id, _, psk = await make_failed(t, age=timedelta(hours=3), checked_ago=timedelta(hours=1))
    env["verdicts"][psk] = verdict("failed", "Insufficient funds")

    stats = await run_payment_reconciliation(isp_operator_id=str(t["operator"].id))
    tx = await load(tx_id)
    assert tx.status == "failed" and tx.failure_reason == "Insufficient funds" and tx.voucher_id is None
    assert stats["late_failed"] == 1

    # It no longer matches the re-check, however long we wait.
    async with async_session_factory() as db:
        row = await db.get(PaymentTransaction, tx_id)
        row.last_status_check_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await db.commit()
    env["asked"].clear()
    await run_payment_reconciliation(isp_operator_id=str(t["operator"].id))
    assert psk not in env["asked"]


@pytest.mark.parametrize("answer", ["pending", "unreachable"])
async def test_no_verdict_leaves_it_failed_and_only_stamps_the_check(env, answer):
    t = await make_tenant()
    tx_id, _, psk = await make_failed(t, age=timedelta(hours=3), checked_ago=timedelta(hours=1))
    env["verdicts"][psk] = verdict("pending") if answer == "pending" else provider_unreachable_result(psk)
    before = datetime.now(timezone.utc)

    await run_payment_reconciliation(isp_operator_id=str(t["operator"].id))

    tx = await load(tx_id)
    # Never revived to pending: the portal must not show it as live again.
    assert tx.status == "failed" and tx.failure_reason == "timeout" and tx.voucher_id is None
    assert tx.last_status_check_at >= before
    assert await status_rows(tx_id) == []


async def test_a_provider_error_is_skipped_and_the_others_are_still_checked(env):
    t = await make_tenant()
    bad_id, _, bad = await make_failed(t, age=timedelta(hours=3), checked_ago=timedelta(hours=1))
    good_id, _, good = await make_failed(t, age=timedelta(hours=3), checked_ago=timedelta(hours=1))
    env["verdicts"][bad] = RuntimeError("provider down")
    env["verdicts"][good] = verdict("success")

    stats = await run_payment_reconciliation(isp_operator_id=str(t["operator"].id))

    assert (await load(bad_id)).status == "failed"
    assert (await load(good_id)).status == "success"
    assert stats["errors"] == 1 and stats["late_success"] == 1


async def test_a_payment_without_a_provider_reference_is_not_asked_about(env):
    t = await make_tenant()
    await make_failed(t, age=timedelta(hours=3), reference=False)
    stats = await run_payment_reconciliation(isp_operator_id=str(t["operator"].id))
    assert stats["late_checked"] == 0


async def test_a_payment_the_provider_failed_is_never_re_checked(env):
    t = await make_tenant()
    _, _, psk = await make_failed(t, age=timedelta(hours=3), reason="Insufficient funds", checked_ago=timedelta(hours=1))
    env["asked"].clear()
    await run_payment_reconciliation(isp_operator_id=str(t["operator"].id))
    assert psk not in env["asked"]


async def test_a_scoped_run_re_checks_only_that_operators_payments(env):
    a, b = await make_tenant(), await make_tenant()
    _, _, a_psk = await make_failed(a, age=timedelta(hours=3), checked_ago=timedelta(hours=1))
    b_id, _, b_psk = await make_failed(b, age=timedelta(hours=3), checked_ago=timedelta(hours=1))
    env["verdicts"][b_psk] = verdict("success")
    env["asked"].clear()

    await run_payment_reconciliation(isp_operator_id=str(a["operator"].id))

    assert a_psk in env["asked"]
    # Negative: the other operator's payment is neither asked about nor settled.
    assert b_psk not in env["asked"] and (await load(b_id)).status == "failed"


# ── webhooks and forced re-checks ─────────────────────────────────────────

async def test_a_success_webhook_settles_a_timed_out_payment_at_any_age(env):
    t = await make_tenant()
    tx_id, ref, _ = await make_failed(t, age=timedelta(days=5))
    env["late"].clear()

    await webhook(t, ref, "success")

    tx = await load(tx_id)
    assert tx.status == "success" and await vouchers_for(ref) == 1
    assert env["sms"].count(tx_id) == 1 and env["late"] == [tx_id]


async def test_a_success_webhook_for_a_payment_the_provider_failed_issues_nothing(env):
    t = await make_tenant()
    tx_id, ref, _ = await make_failed(t, age=timedelta(hours=3), reason="Insufficient funds")
    env["contradicted"].clear()

    await webhook(t, ref, "success")

    tx = await load(tx_id)
    assert tx.status == "failed" and tx.failure_reason == "Insufficient funds" and tx.voucher_id is None
    assert await vouchers_for(ref) == 0 and tx_id not in env["sms"]
    assert env["contradicted"] == [tx_id]


async def test_a_failed_webhook_leaves_a_timed_out_payment_to_the_re_check(env):
    t = await make_tenant()
    tx_id, ref, _ = await make_failed(t, age=timedelta(hours=3))
    await webhook(t, ref, "failed")
    tx = await load(tx_id)
    assert tx.status == "failed" and tx.failure_reason == "timeout"


async def test_a_flutterwave_webhook_forces_a_re_check_of_a_timed_out_payment(env):
    t = await make_tenant()
    tx_id, ref, psk = await make_failed(t, age=timedelta(days=3))
    env["verdicts"][psk] = verdict("success")
    env["asked"].clear()

    await webhook(t, ref, "success", provider="flutterwave")

    assert env["asked"] == [psk]
    assert (await load(tx_id)).status == "success" and await vouchers_for(ref) == 1


async def test_only_a_forced_refresh_asks_about_a_timed_out_payment(env):
    """The portal's poll (not forced) stays quiet; an operator's Re-check (forced) asks."""
    t = await make_tenant()
    tx_id, ref, psk = await make_failed(t, age=timedelta(hours=3))
    env["verdicts"][psk] = verdict("success")
    service = PaymentService()
    env["asked"].clear()

    async with async_session_factory() as db:
        tx = await db.get(PaymentTransaction, tx_id)
        await service.refresh_transaction_status(db, tx=tx, force=False)
    assert env["asked"] == [] and (await load(tx_id)).status == "failed"

    async with async_session_factory() as db:
        tx = await db.get(PaymentTransaction, tx_id)
        await service.refresh_transaction_status(db, tx=tx, force=True)
    assert env["asked"] == [psk] and (await load(tx_id)).status == "success"


async def test_a_forced_refresh_never_asks_about_a_payment_the_provider_failed(env):
    t = await make_tenant()
    tx_id, _, psk = await make_failed(t, age=timedelta(hours=3), reason="Insufficient funds")
    env["verdicts"][psk] = verdict("success")
    env["asked"].clear()
    async with async_session_factory() as db:
        tx = await db.get(PaymentTransaction, tx_id)
        await PaymentService().refresh_transaction_status(db, tx=tx, force=True)
    assert env["asked"] == [] and (await load(tx_id)).status == "failed"


async def test_a_poll_and_a_webhook_together_issue_one_voucher(env):
    t = await make_tenant()
    service = PaymentService()

    async def confirm(ref, trigger):
        async with async_session_factory() as db:
            return await service.resolve_successful_payment(db, internal_reference=ref, trigger_source=trigger)

    for _ in range(10):
        tx_id, ref, _ = await make_failed(t, age=timedelta(hours=3))
        env["sms"].clear()
        env["late"].clear()
        results = await asyncio.gather(confirm(ref, "poll"), confirm(ref, "webhook"))
        assert {r.status for r in results} == {"success"}
        assert await vouchers_for(ref) == 1
        assert env["sms"] == [tx_id] and env["late"] == [tx_id]
        assert len(await status_rows(tx_id)) == 1


# ── the operator's email ──────────────────────────────────────────────────

class _Mailbox:
    def __init__(self):
        self.sent = []

    async def send(self, *, to, subject, body_html, body_text):
        self.sent.append({"to": to, "subject": subject, "html": body_html, "text": body_text})


async def _real_alert(monkeypatch, name, tx_id):
    box = _Mailbox()
    monkeypatch.setattr(operator_alerts, "get_email_service", lambda: box)
    real = getattr(operator_alerts, "_late_success" if name == "late" else "_success_after_failure")
    await operator_alerts._send(tx_id, name, real)
    return box.sent


async def test_the_late_success_email_names_the_payment_but_never_the_phone_or_code(env, monkeypatch):
    t = await make_tenant()
    tx_id, ref, psk = await make_failed(t, age=timedelta(hours=3), checked_ago=timedelta(hours=1))
    env["verdicts"][psk] = verdict("success")
    await run_payment_reconciliation(isp_operator_id=str(t["operator"].id))
    async with async_session_factory() as db:
        code = (await db.get(Voucher, (await db.get(PaymentTransaction, tx_id)).voucher_id)).code

    [mail] = await _real_alert(monkeypatch, "late", tx_id)

    assert mail["to"] == t["operator"].contact_email
    assert "GHS 2.00" in mail["subject"]
    for body in (mail["text"], mail["html"]):
        assert ref in body and "sent it to the customer by SMS" in body
        assert code not in body and PHONE not in body and PHONE[3:] not in body
    assert code[-4:] in mail["text"]


async def test_without_a_phone_the_email_says_where_to_find_the_voucher(env, monkeypatch):
    t = await make_tenant()
    tx_id, ref, _ = await make_failed(t, age=timedelta(hours=3), phone=None)
    await webhook(t, ref, "success")
    assert tx_id not in env["sms"]
    [mail] = await _real_alert(monkeypatch, "late", tx_id)
    assert "no phone number" in mail["text"] and "under Vouchers" in mail["text"]


async def test_the_contradiction_email_says_nothing_was_issued(env, monkeypatch):
    t = await make_tenant()
    tx_id, ref, _ = await make_failed(t, age=timedelta(hours=3), reason="Insufficient funds")
    [mail] = await _real_alert(monkeypatch, "contradicted", tx_id)
    assert "No voucher was issued" in mail["text"] and "Insufficient funds" in mail["text"] and ref in mail["text"]


async def test_a_diagnostic_payment_emails_nobody(env, monkeypatch):
    t = await make_tenant()
    tx_id, _, _ = await make_failed(t, age=timedelta(hours=3))
    async with async_session_factory() as db:
        (await db.get(PaymentTransaction, tx_id)).is_diagnostic = True
        await db.commit()
    assert await _real_alert(monkeypatch, "late", tx_id) == []


async def test_a_failed_send_never_raises(env, monkeypatch):
    t = await make_tenant()
    tx_id, _, _ = await make_failed(t, age=timedelta(hours=3))

    class Broken:
        async def send(self, **_):
            raise RuntimeError("smtp down")

    monkeypatch.setattr(operator_alerts, "get_email_service", lambda: Broken())
    await operator_alerts._send(tx_id, "late", operator_alerts._late_success)  # no exception
