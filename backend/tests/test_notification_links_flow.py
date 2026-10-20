"""The links in the six lifecycle notifications (application approved, trial
warning, trial expired, invoice issued, grace period, suspended) against a
REAL database: they are built on the platform_app_url row a platform owner
edits, not on .env, and fall back to .env when the row is empty or the lookup
fails.

The two job-sent ones (invoice issued, grace period) go through the real
invoice_generation and billing_enforcement jobs; the rest are called directly.
Rendering and sending are replaced by a recorder, so what is checked is the
value each template receives.

Only runs against a disposable database:

    NOTIFICATION_LINKS_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>

(tests/throwaway_db.py run sets both.)
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("NOTIFICATION_LINKS_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs NOTIFICATION_LINKS_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    from src.config import get_settings
    from src.db import base as db_base
    from src.db.base import async_session_factory
    from src.db.models import ISPOperator, OperatorInvoice
    from src.modules.notifications import dispatcher
    from src.modules.platform.settings_service import set_setting

DB_URL = "https://links.throwaway.test"
ENV_URL = "http://10.9.9.9"


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    rendered: list[tuple[str, dict]] = []

    async def render_email(event, values):
        rendered.append((event, dict(values)))
        return "subject", "<p>html</p>", "text"

    async def render_sms(event, values):
        return "sms"

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(dispatcher.store, "render_email", render_email)
    monkeypatch.setattr(dispatcher.store, "render_sms", render_sms)
    monkeypatch.setattr(dispatcher, "_send_email", nothing)
    monkeypatch.setattr(dispatcher, "_send_sms", nothing)
    # The .env value, distinct from the row, so a link shows which it came from.
    # get_settings() is cached: the dispatcher and settings_service both read this object.
    monkeypatch.setattr(get_settings(), "platform_app_url", ENV_URL)
    yield {"rendered": rendered}


async def set_app_url(value: str) -> None:
    async with async_session_factory() as db:
        await set_setting(db, "platform_app_url", value)
        await db.commit()


async def make_operator(**values) -> "ISPOperator":
    slug = f"lnk-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Lnk {slug}", slug=slug, contact_email=f"{slug}@throwaway.test",
                               contact_phone="233244000111", monthly_fee_ghs=Decimal("50.00"), status="approved",
                               billing_status="active", **values)
        db.add(operator)
        await db.commit()
        return operator


async def make_overdue_invoice(operator) -> "OperatorInvoice":
    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        invoice = OperatorInvoice(isp_operator_id=operator.id, invoice_number=f"INV-LNK-{uuid.uuid4().hex[:8]}",
                                  period_start=now - timedelta(days=60), period_end=now - timedelta(days=30),
                                  amount_ghs=Decimal("50.00"), status="issued", issued_at=now - timedelta(days=60),
                                  due_at=now - timedelta(days=1))
        db.add(invoice)
        await db.commit()
        return invoice


async def send_all_six(env) -> dict[str, dict]:
    """Every one of the six, as it is sent in production; returns the values
    each template got, by event."""
    from src.jobs.billing_enforcement import enforce_billing
    from src.jobs.invoice_generation import generate_monthly_invoices

    env["rendered"].clear()
    await dispatcher.notify_application_approved(
        email="a@throwaway.test", phone="", contact_name="A", isp_name="A ISP", admin_email="a@throwaway.test",
        temp_password="Temp-123", trial_days=14,
    )
    await dispatcher.notify_trial_expiry_warning(
        email="a@throwaway.test", phone="", isp_name="A ISP", trial_end_date="1 Jan", days_remaining=3,
        monthly_fee_ghs=Decimal("50.00"),
    )
    await dispatcher.notify_trial_expired(email="a@throwaway.test", phone="", isp_name="A ISP")
    await dispatcher.notify_suspended(email="a@throwaway.test", phone="", isp_name="A ISP")

    invoiced = await make_operator()
    await generate_monthly_invoices()
    late = await make_operator()
    await make_overdue_invoice(late)
    await enforce_billing()

    by_event: dict[str, dict] = {}
    for event, values in env["rendered"]:
        if event == "invoice_issued" and values["isp_name"] != invoiced.name:
            continue  # other operators in the database (tenant-zero) are invoiced too
        if event == "grace_period" and values["isp_name"] != late.name:
            continue
        by_event[event] = values
    return by_event


LINKS = {
    "application_approved": ("login_url", "/admin"),
    "trial_expiry_warning": ("billing_url", "/admin/billing"),
    "trial_expired": ("billing_url", "/admin/billing"),
    "account_suspended": ("payment_url", "/admin/billing"),
    "invoice_issued": ("payment_url", "/admin/billing"),
    "grace_period": ("payment_url", "/admin/billing"),
}


async def test_all_six_links_come_from_the_database_setting(env):
    # Trailing slash as a platform owner might type it: no "//admin".
    await set_app_url(DB_URL + "/")
    sent = await send_all_six(env)
    assert set(sent) == set(LINKS)
    for event, (key, path) in LINKS.items():
        assert sent[event][key] == DB_URL + path, event


async def test_a_changed_setting_is_used_on_the_next_send(env):
    """No caching: the settings page takes effect at once."""
    await set_app_url("https://changed.throwaway.test")
    await dispatcher.notify_trial_expired(email="a@throwaway.test", phone="", isp_name="A ISP")
    assert env["rendered"][-1][1]["billing_url"] == "https://changed.throwaway.test/admin/billing"


async def test_empty_setting_falls_back_to_env(env):
    """Negative: with no value in the row, the .env value is used — never an
    empty base that would make the link relative."""
    await set_app_url("")
    sent = await send_all_six(env)
    for event, (key, path) in LINKS.items():
        assert sent[event][key] == ENV_URL + path, event


async def test_failed_lookup_falls_back_to_env_and_still_sends(env, monkeypatch):
    """Negative: a database error degrades the link, not the notification."""
    await set_app_url(DB_URL)

    def broken():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db_base, "async_session_factory", broken)
    env["rendered"].clear()
    await dispatcher.notify_application_approved(
        email="a@throwaway.test", phone="", contact_name="A", isp_name="A ISP", admin_email="a@throwaway.test",
        temp_password="Temp-123", trial_days=14,
    )
    [(event, values)] = env["rendered"]
    assert event == "application_approved" and values["login_url"] == ENV_URL + "/admin"
    assert values["temp_password"] == "Temp-123"
