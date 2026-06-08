"""
test_billing.py -- Phase 2 billing and operator lifecycle tests.
All 10 spec tests must pass before Phase 2 is complete.
"""
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import asyncio
import pytest

from test_multi_tenant_security import _request


def run_async(coro):
    """Run an async coroutine and dispose the DB pool afterward to avoid
    'Future attached to a different loop' errors between sequential tests."""
    try:
        return asyncio.run(coro)
    finally:
        try:
            from src.db.base import engine
            engine.sync_engine.dispose()
        except Exception:
            pass

PLATFORM_OWNER_EMAIL = os.getenv("PLATFORM_OWNER_EMAIL", "owner@yourisp.com")
PLATFORM_OWNER_PASSWORD = os.getenv("PLATFORM_OWNER_PASSWORD", "ChangeMe2024Strong!")

_PLATFORM_TOKEN: str | None = None


def _login_platform_owner() -> str:
    global _PLATFORM_TOKEN
    if _PLATFORM_TOKEN:
        return _PLATFORM_TOKEN
    status, body = _request(
        "POST",
        "/api/v1/platform/auth/login",
        body={"email": PLATFORM_OWNER_EMAIL, "password": PLATFORM_OWNER_PASSWORD},
    )
    assert status == 200, body
    _PLATFORM_TOKEN = body["access_token"]
    return _PLATFORM_TOKEN


def _unique(prefix: str) -> str:
    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _submit_application(isp_name: str, email: str) -> dict:
    status, body = _request(
        "POST",
        "/api/v1/public/apply",
        body={
            "isp_name": isp_name,
            "contact_name": "Test Applicant",
            "email": email,
            "phone": "0244123456",
            "region": "Greater Accra",
            "expected_sites": 2,
            "message": "Test application",
        },
    )
    assert status == 201, body
    return body


def _approve_application(app_id: str, monthly_fee_ghs: float = 200.0) -> dict:
    owner = _login_platform_owner()
    status, body = _request(
        "PUT",
        f"/api/v1/platform/applications/{app_id}/approve",
        token=owner,
        body={"monthly_fee_ghs": monthly_fee_ghs},
    )
    assert status == 200, body
    return body


def _reject_application(app_id: str, reason: str = "Not eligible") -> dict:
    owner = _login_platform_owner()
    status, body = _request(
        "PUT",
        f"/api/v1/platform/applications/{app_id}/reject",
        token=owner,
        body={"rejection_reason": reason},
    )
    assert status == 200, body
    return body


def _login_admin(email: str, password: str) -> str:
    status, body = _request("POST", "/api/v1/auth/login", body={"email": email, "password": password})
    assert status == 200, body
    return body["access_token"]


# ---------------------------------------------------------------------------
# Test 1: Submit application
# ---------------------------------------------------------------------------

def test_submit_application_creates_row():
    """Spec #1 -- Submit application â†’ confirm row created, notification logged."""
    isp_name = _unique("TestISP")
    email = f"{_unique('apply')}@test.com"
    result = _submit_application(isp_name, email)
    assert "id" in result
    assert "received" in result["message"].lower()

    owner = _login_platform_owner()
    status, apps = _request("GET", "/api/v1/platform/applications", token=owner)
    assert status == 200
    found = next((a for a in apps if a["id"] == result["id"]), None)
    assert found is not None
    assert found["status"] == "pending"
    assert found["isp_name"] == isp_name


# ---------------------------------------------------------------------------
# Test 2: Approve application
# ---------------------------------------------------------------------------

def test_approve_application_creates_operator_and_admin():
    """Spec #2 -- Approve â†’ operator created, admin created, trial started."""
    isp_name = _unique("ApproveISP")
    email = f"{_unique('approv')}@test.com"
    sub = _submit_application(isp_name, email)

    result = _approve_application(sub["id"])
    assert "operator_id" in result
    assert "temp_password" in result
    assert result["temp_password"]  # Must be non-empty, shown once
    assert "trial_ends_at" in result

    # Admin must be able to log in
    admin_token = _login_admin(email, result["temp_password"])
    assert admin_token

    # Verify billing_status via billing status endpoint
    status, billing = _request("GET", "/api/v1/billing/status", token=admin_token)
    assert status == 200, billing
    assert billing["billing_status"] == "trial"


# ---------------------------------------------------------------------------
# Test 3: Reject application
# ---------------------------------------------------------------------------

def test_reject_application_creates_no_operator():
    """Spec #3 -- Reject â†’ no operator created, rejection notification sent."""
    isp_name = _unique("RejectISP")
    email = f"{_unique('reject')}@test.com"
    sub = _submit_application(isp_name, email)

    result = _reject_application(sub["id"], "Not operating in supported region")
    assert result["message"]

    # Verify status updated
    owner = _login_platform_owner()
    status, app = _request("GET", f"/api/v1/platform/applications/{sub['id']}", token=owner)
    assert status == 200
    assert app["status"] == "rejected"
    assert app["isp_operator_id"] is None  # No operator created


# ---------------------------------------------------------------------------
# Test 4: Trial expiry warning (job simulation)
# ---------------------------------------------------------------------------

def test_trial_expiry_warning_job():
    """Spec #4 -- Set trial_ends_at to 2 days from now, run job, confirm warning event."""
    from src.db.base import async_session_factory
    from src.db.models import ISPOperator, OperatorBillingEvent
    from src.jobs.trial_expiry import handle_trial_expiry
    from sqlalchemy import select
    import asyncio

    isp_name = _unique("WarningISP")
    email = f"{_unique('warn')}@test.com"
    sub = _submit_application(isp_name, email)
    result = _approve_application(sub["id"])
    operator_id = result["operator_id"]

    async def run():
        async with async_session_factory() as db:
            op = await db.get(ISPOperator, operator_id)
            op.trial_ends_at = datetime.now(timezone.utc) + timedelta(days=2)
            await db.commit()

        await handle_trial_expiry()

        async with async_session_factory() as db:
            events = (await db.execute(
                select(OperatorBillingEvent).where(
                    OperatorBillingEvent.isp_operator_id == operator_id,
                    OperatorBillingEvent.event_type == "trial_expiry_warning",
                )
            )).scalars().all()
            assert len(events) >= 1, "trial_expiry_warning billing event not created"

    run_async(run())


# ---------------------------------------------------------------------------
# Test 5: Trial expiry â†’ first invoice generated
# ---------------------------------------------------------------------------

def test_trial_expiry_generates_first_invoice():
    """Spec #5 -- Set trial_ends_at to yesterday, run job, confirm invoice + billing_status='active'."""
    from src.db.base import async_session_factory
    from src.db.models import ISPOperator, OperatorInvoice
    from src.jobs.trial_expiry import handle_trial_expiry
    from sqlalchemy import select
    import asyncio

    isp_name = _unique("ExpiredISP")
    email = f"{_unique('expir')}@test.com"
    sub = _submit_application(isp_name, email)
    result = _approve_application(sub["id"])
    operator_id = result["operator_id"]
    admin_token = _login_admin(email, result["temp_password"])

    async def run():
        async with async_session_factory() as db:
            op = await db.get(ISPOperator, operator_id)
            op.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=1)
            await db.commit()

        await handle_trial_expiry()

        async with async_session_factory() as db:
            op = await db.get(ISPOperator, operator_id)
            assert op.billing_status == "active", f"Expected active, got {op.billing_status}"

            invoices = (await db.execute(
                select(OperatorInvoice).where(OperatorInvoice.isp_operator_id == operator_id)
            )).scalars().all()
            assert len(invoices) >= 1, "No invoice generated after trial expiry"
            assert invoices[0].status == "issued"

    run_async(run())

    status, billing = _request("GET", "/api/v1/billing/status", token=admin_token)
    assert status == 200
    assert billing["billing_status"] == "active"


# ---------------------------------------------------------------------------
# Test 6: Invoice payment -- Paystack initialize called with platform billing keys
# ---------------------------------------------------------------------------

def test_invoice_payment_uses_platform_billing_keys():
    """Spec #6 -- Initiate payment, confirm Paystack called with platform billing keys."""
    import httpx
    import unittest.mock as mock
    from src.db.models import ISPOperator
    from src.db.base import async_session_factory
    from src.modules.billing.service import create_invoice, initiate_invoice_payment
    from src.config import get_settings
    from datetime import date

    isp_name = _unique("PayISP")
    email = f"{_unique('pay')}@test.com"
    sub = _submit_application(isp_name, email)
    result = _approve_application(sub["id"])
    operator_id = result["operator_id"]
    settings = get_settings()

    seen = {}

    async def fake_handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("Authorization", "")
        seen["path"] = request.url.path
        return httpx.Response(200, json={
            "status": True,
            "data": {
                "authorization_url": "https://checkout.paystack.com/test",
                "reference": "test-ref-001",
            },
        })

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(fake_handler))
            super().__init__(**kwargs)

    async def run():
        async with async_session_factory() as db:
            op = await db.get(ISPOperator, operator_id)
            today = date.today()
            invoice = await create_invoice(db, op, today.replace(day=1), today)
            await db.commit()
            await db.refresh(invoice)
            await db.refresh(op)

            fake_key = "sk_test_platform_billing_test_key"
            with mock.patch("src.modules.billing.service.get_settings") as mock_s:
                mock_s.return_value.platform_billing_paystack_secret_key = fake_key
                mock_s.return_value.platform_app_url = "http://localhost:8000"
                with mock.patch.object(httpx, "AsyncClient", PatchedClient):
                    redirect_url = await initiate_invoice_payment(db, invoice, op)

        assert seen.get("path") == "/transaction/initialize"
        assert seen["authorization"] == "Bearer sk_test_platform_billing_test_key"
        assert "checkout.paystack.com" in redirect_url or "paystack" in redirect_url.lower()

    run_async(run())


# ---------------------------------------------------------------------------
# Test 7: Platform billing webhook marks invoice paid, reactivates suspended operator
# ---------------------------------------------------------------------------

def test_platform_billing_webhook_marks_paid_and_reactivates():
    """Spec #7 -- POST webhook, confirm invoice paid, suspended operator reactivated."""
    try:
        from src.db.base import engine
        engine.sync_engine.dispose()
    except Exception:
        pass
    from src.db.base import async_session_factory
    from src.db.models import ISPOperator, OperatorInvoice
    from src.modules.billing.service import create_invoice
    from datetime import date
    import asyncio, hmac, hashlib, json

    isp_name = _unique("WebhookISP")
    email = f"{_unique('whk')}@test.com"
    sub = _submit_application(isp_name, email)
    result = _approve_application(sub["id"])
    operator_id = result["operator_id"]
    slug = result["slug"]

    invoice_id = None

    async def setup():
        nonlocal invoice_id
        async with async_session_factory() as db:
            op = await db.get(ISPOperator, operator_id)
            op.status = "suspended"
            op.billing_status = "past_due"
            today = date.today()
            invoice = await create_invoice(db, op, today.replace(day=1), today)
            await db.commit()
            invoice_id = str(invoice.id)

    run_async(setup())

    payload = {
        "event": "charge.success",
        "data": {
            "reference": f"INV-{invoice_id}",
            "metadata": {
                "invoice_id": invoice_id,
                "operator_id": operator_id,
            },
        },
    }
    raw = json.dumps(payload).encode()
    from src.config import get_settings
    secret = get_settings().platform_billing_paystack_webhook_secret or "test-secret"
    sig = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()

    # Post to platform billing webhook
    import urllib.request, urllib.error
    url = f"{os.getenv('TEST_BASE_URL', 'http://localhost:8000')}/api/v1/webhooks/platform-billing/paystack"
    req = urllib.request.Request(
        url=url,
        data=raw,
        headers={"Content-Type": "application/json", "x-paystack-signature": sig},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        status = e.code
        body = json.loads(e.read().decode())

    assert status == 200, body

    async def verify():
        async with async_session_factory() as db:
            from sqlalchemy import select
            invoice = (await db.execute(
                select(OperatorInvoice).where(OperatorInvoice.id == invoice_id)
            )).scalar_one_or_none()
            assert invoice is not None
            assert invoice.status == "paid"

            op = await db.get(ISPOperator, operator_id)
            assert op.status == "approved"
            assert op.billing_status == "active"

    run_async(verify())


# ---------------------------------------------------------------------------
# Test 8: Suspended operator cannot generate vouchers
# ---------------------------------------------------------------------------

def test_suspended_operator_cannot_generate_vouchers():
    """Spec #8 -- Suspended operator gets 403 on voucher generate."""
    from src.db.base import async_session_factory
    from src.db.models import ISPOperator
    import asyncio

    isp_name = _unique("SuspendISP")
    email = f"{_unique('susp')}@test.com"
    sub = _submit_application(isp_name, email)
    result = _approve_application(sub["id"])
    operator_id = result["operator_id"]
    admin_token = _login_admin(email, result["temp_password"])

    async def suspend():
        async with async_session_factory() as db:
            op = await db.get(ISPOperator, operator_id)
            op.status = "suspended"
            await db.commit()

    run_async(suspend())

    status, body = _request(
        "POST",
        "/api/v1/vouchers/generate",
        token=admin_token,
        body={"plan_id": str(uuid.uuid4()), "site_id": str(uuid.uuid4()), "quantity": 1, "device_policy": "single"},
    )
    assert status == 403, f"Expected 403, got {status}: {body}"
    assert "suspended" in body.get("detail", "").lower()


# ---------------------------------------------------------------------------
# Test 9: Suspended operator can still view vouchers
# ---------------------------------------------------------------------------

def test_suspended_operator_can_view_vouchers():
    """Spec #9 -- Suspended operator can still GET /vouchers â†’ 200."""
    from src.db.base import async_session_factory
    from src.db.models import ISPOperator
    import asyncio

    isp_name = _unique("SuspView")
    email = f"{_unique('sv')}@test.com"
    sub = _submit_application(isp_name, email)
    result = _approve_application(sub["id"])
    operator_id = result["operator_id"]
    admin_token = _login_admin(email, result["temp_password"])

    async def suspend():
        async with async_session_factory() as db:
            op = await db.get(ISPOperator, operator_id)
            op.status = "suspended"
            await db.commit()

    run_async(suspend())

    status, body = _request("GET", "/api/v1/vouchers", token=admin_token)
    assert status == 200, f"Expected 200, got {status}: {body}"


# ---------------------------------------------------------------------------
# Test 10: Grace period enforcement -- overdue invoice past grace period â†’ suspended
# ---------------------------------------------------------------------------

def test_grace_period_enforcement_suspends_operator():
    """Spec #10 -- Invoice due_at 15 days ago â†’ operator suspended after job."""
    from src.db.base import async_session_factory
    from src.db.models import ISPOperator, OperatorInvoice
    from src.jobs.billing_enforcement import enforce_billing
    from src.modules.billing.service import create_invoice
    from datetime import date
    from sqlalchemy import select
    import asyncio

    isp_name = _unique("GraceISP")
    email = f"{_unique('grace')}@test.com"
    sub = _submit_application(isp_name, email)
    result = _approve_application(sub["id"])
    operator_id = result["operator_id"]

    async def setup_and_run():
        async with async_session_factory() as db:
            op = await db.get(ISPOperator, operator_id)
            op.billing_status = "active"
            today = date.today()
            invoice = await create_invoice(db, op, today.replace(day=1), today)
            # Back-date due_at and set to overdue
            invoice.status = "overdue"
            invoice.due_at = datetime.now(timezone.utc) - timedelta(days=15)
            await db.commit()

        await enforce_billing()

        async with async_session_factory() as db:
            op = await db.get(ISPOperator, operator_id)
            assert op.status == "suspended", f"Expected suspended, got {op.status}"
            assert op.billing_status == "past_due"

    run_async(setup_and_run())
