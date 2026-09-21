"""The exact response fields the two template editor pages read.

These pages are hand-written fetch calls, not generated clients: the operator
editor is frontend/src/components/VoucherSmsTemplate.jsx and the platform one is
backend/src/platform_portal/notification_templates.html. Neither fails loudly if
a field is renamed — a missing key renders as "undefined" or an empty counter,
which looks like a styling bug rather than a broken contract. Every field named
below is one of those pages reading it, so a rename breaks a test instead.

Same throwaway-database guard as the other flow suites.
"""
from __future__ import annotations

import os
import uuid

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
    from src.db.models import PlatformOwner
    from src.modules.notifications import template_catalog as catalog
    from src.utils.auth import hash_password

OWNER_PASSWORD = "Owner-Passw0rd"
PLATFORM_BASE = "/platform/notification-templates"


@pytest_asyncio.fixture(loop_scope="module")
async def owner():
    email = f"ui-owner-{uuid.uuid4().hex[:8]}@throwaway.test"
    async with async_session_factory() as db:
        db.add(PlatformOwner(email=email, password_hash=hash_password(OWNER_PASSWORD), name="Owner", is_active=True))
        await db.commit()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as c:
        res = await c.post("/platform/auth/login", json={"email": email, "password": OWNER_PASSWORD})
        assert res.status_code == 200, res.text
        c.headers.update({"Authorization": f"Bearer {res.json()['access_token']}"})
        yield c


# ── platform portal: notification_templates.html ──────────────────────────

async def test_list_carries_every_field_the_event_list_renders(owner):
    """renderList() reads event, channel, label, description, is_default."""
    items = (await owner.get(PLATFORM_BASE)).json()["templates"]
    assert items
    for item in items:
        for field in ("event", "channel", "label", "description", "is_default"):
            assert field in item, f"{item.get('event')}/{item.get('channel')} missing {field}"
        assert item["channel"] in ("email", "sms")


async def test_get_carries_every_field_the_editor_fills(owner):
    """fillEditor() reads subject, body_text, body_html, placeholders[],
    max_sms_segments and preview."""
    for channel in ("email", "sms"):
        body = (await owner.get(f"{PLATFORM_BASE}/application_approved/{channel}")).json()
        for field in ("subject", "body_text", "body_html", "placeholders", "max_sms_segments", "preview", "is_default"):
            assert field in body, f"{channel} missing {field}"
        for placeholder in body["placeholders"]:
            assert set(placeholder) >= {"key", "description", "required"}
        assert (body["max_sms_segments"] is None) is (channel == "email")


async def test_the_approval_template_marks_temp_password_required(owner):
    """missingRequired() drives the Save button off this flag alone."""
    for channel in ("email", "sms"):
        body = (await owner.get(f"{PLATFORM_BASE}/application_approved/{channel}")).json()
        required = [p["key"] for p in body["placeholders"] if p["required"]]
        assert required == ["temp_password"]


async def test_no_other_template_claims_a_required_placeholder(owner):
    """Anything else marked required would silently disable Save on that page."""
    for item in (await owner.get(PLATFORM_BASE)).json()["templates"]:
        if item["event"] == "application_approved":
            continue
        assert [p["key"] for p in item["placeholders"] if p["required"]] == []


async def test_sms_preview_carries_exactly_what_measure_reads(owner):
    """measure() reads channel, character_count, segment_count, encoding, body_text."""
    res = await owner.post(
        f"{PLATFORM_BASE}/application_approved/sms/preview",
        json={"body_text": "Approved. {temp_password} at {login_url}"},
    )
    body = res.json()
    assert set(body) >= {"valid", "error", "preview"}
    assert body["valid"] is True
    preview = body["preview"]
    assert preview["channel"] == "sms"
    for field in ("character_count", "segment_count", "encoding", "body_text"):
        assert field in preview
    assert preview["encoding"] in ("gsm7", "ucs2")
    assert isinstance(preview["segment_count"], int)


async def test_email_preview_carries_exactly_what_measure_reads(owner):
    """measure() reads subject, body_html and body_text for the email branch."""
    default = catalog.DEFAULTS[("application_approved", "email")]
    res = await owner.post(
        f"{PLATFORM_BASE}/application_approved/email/preview",
        json={"subject": default.subject, "body_text": default.body_text, "body_html": default.body_html},
    )
    preview = res.json()["preview"]
    assert preview["channel"] == "email"
    for field in ("subject", "body_html", "body_text", "character_count"):
        assert field in preview
    # srcdoc gets a whole document, not a fragment.
    assert preview["body_html"].startswith("<html")


async def test_an_invalid_draft_returns_200_with_a_readable_error(owner):
    """runPreview() shows data.error inline; a non-200 would fall into the
    network-error branch and say nothing useful."""
    res = await owner.post(
        f"{PLATFORM_BASE}/application_approved/sms/preview", json={"body_text": "No placeholder here."}
    )
    assert res.status_code == 200
    assert res.json()["valid"] is False
    assert "{temp_password}" in res.json()["error"]


async def test_a_refused_save_puts_its_reason_in_detail(owner):
    """The save handler shows data.detail — FastAPI's field, not 'error'."""
    res = await owner.put(
        f"{PLATFORM_BASE}/application_approved/sms", json={"body_text": "Approved, no password."}
    )
    assert res.status_code == 400
    assert "{temp_password}" in res.json()["detail"]


async def test_save_and_reset_both_return_a_full_editor_payload(owner):
    """replaceTemplate() re-fills the editor straight from the response."""
    saved = (await owner.put(
        f"{PLATFORM_BASE}/trial_expired/sms",
        json={"body_text": "Trial over. Pay at {billing_url}"},
    )).json()
    assert saved["is_default"] is False
    assert {"placeholders", "preview", "body_text", "max_sms_segments"} <= set(saved)

    reset = (await owner.post(f"{PLATFORM_BASE}/trial_expired/sms/reset")).json()
    assert reset["is_default"] is True
    assert {"placeholders", "preview", "body_text"} <= set(reset)
    assert reset["body_text"] == catalog.DEFAULTS[("trial_expired", "sms")].body_text
