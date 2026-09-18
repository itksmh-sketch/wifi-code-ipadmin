"""Platform notification template endpoints against a REAL database.

Same guard as the other flow suites: writes platform_notification_templates
rows, so it only runs when explicitly pointed at a disposable database
(ADMIN_FLOW_TEST_DATABASE_URL, whose name must contain "throwaway", equal to
DATABASE_URL) and skips everywhere else — including the production container.
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
    from src.db.models import PlatformNotificationTemplate, PlatformOwner
    from src.modules.notifications import template_catalog as catalog
    from src.modules.notifications import template_store
    from src.utils.auth import hash_password

OWNER_PASSWORD = "Owner-Passw0rd"


@pytest_asyncio.fixture(loop_scope="module")
async def client():
    """A signed-in platform owner. No operator or admin is needed: every endpoint
    under test is platform-scoped."""
    owner_email = f"tpl-owner-{uuid.uuid4().hex[:8]}@throwaway.test"
    async with async_session_factory() as db:
        db.add(PlatformOwner(email=owner_email, password_hash=hash_password(OWNER_PASSWORD), name="Owner", is_active=True))
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as c:
        res = await c.post("/platform/auth/login", json={"email": owner_email, "password": OWNER_PASSWORD})
        assert res.status_code == 200, res.text
        c.owner_headers = {"Authorization": f"Bearer {res.json()['access_token']}"}
        async with async_session_factory() as db:
            c.owner_id = (await db.execute(select(PlatformOwner.id).where(PlatformOwner.email == owner_email))).scalar_one()
        yield c, None

BASE = "/platform/notification-templates"
APPROVED = "application_approved"


async def _row(event, channel):
    async with async_session_factory() as db:
        return (
            await db.execute(
                select(PlatformNotificationTemplate).where(
                    PlatformNotificationTemplate.event_type == event,
                    PlatformNotificationTemplate.channel == channel,
                )
            )
        ).scalar_one_or_none()


async def _reset(c, headers, event, channel):
    assert (await c.post(f"{BASE}/{event}/{channel}/reset", headers=headers)).status_code == 200


# ── read ──────────────────────────────────────────────────────────────────

async def test_listing_returns_every_event_and_channel_seeded_as_default(client):
    c, _ = client
    res = await c.get(BASE, headers=c.owner_headers)
    assert res.status_code == 200
    items = res.json()["templates"]
    assert len(items) == len(catalog.EVENTS) * len(catalog.CHANNELS)
    assert {(i["event"], i["channel"]) for i in items} == set(catalog.DEFAULTS)
    assert all(i["is_default"] for i in items), "a freshly migrated platform is entirely on defaults"
    assert all(i["label"] and i["description"] for i in items)


async def test_the_seeded_row_is_the_shipped_default_text(client):
    c, _ = client
    for (event, channel), default in catalog.DEFAULTS.items():
        row = await _row(event, channel)
        assert row is not None, f"migration 044 did not seed {event}/{channel}"
        assert row.body_text == default.body_text
        assert (row.subject or None) == default.subject
        assert (row.body_html or None) == default.body_html


async def test_get_one_template_exposes_placeholders_and_a_preview(client):
    c, _ = client
    res = await c.get(f"{BASE}/{APPROVED}/sms", headers=c.owner_headers)
    assert res.status_code == 200
    body = res.json()
    keys = {p["key"] for p in body["placeholders"]}
    assert keys == set(catalog.EVENTS[APPROVED].placeholders)
    assert [p["key"] for p in body["placeholders"] if p["required"]] == ["temp_password"]
    assert body["max_sms_segments"] == catalog.MAX_SMS_SEGMENTS
    assert body["preview"]["segment_count"] >= 1
    assert "{" not in body["preview"]["body_text"]
    assert body["default"]["body_text"] == catalog.DEFAULTS[(APPROVED, "sms")].body_text


async def test_an_email_preview_carries_a_wrapped_html_document(client):
    c, _ = client
    body = (await c.get(f"{BASE}/{APPROVED}/email", headers=c.owner_headers)).json()
    assert body["preview"]["body_html"].startswith("<html><body")
    assert body["max_sms_segments"] is None


@pytest.mark.parametrize("path", [f"{BASE}/no_such_event/sms", f"{BASE}/{APPROVED}/pigeon"])
async def test_unknown_event_or_channel_is_404(client, path):
    c, _ = client
    assert (await c.get(path, headers=c.owner_headers)).status_code == 404


async def test_every_endpoint_requires_a_platform_owner_token(client):
    c, _ = client
    assert (await c.get(BASE)).status_code in (401, 403)
    assert (await c.get(f"{BASE}/{APPROVED}/sms")).status_code in (401, 403)
    assert (await c.put(f"{BASE}/{APPROVED}/sms", json={"body_text": "x {temp_password}"})).status_code in (401, 403)
    assert (await c.post(f"{BASE}/{APPROVED}/sms/reset")).status_code in (401, 403)


# ── preview without saving ────────────────────────────────────────────────

async def test_preview_measures_without_touching_the_stored_row(client):
    c, _ = client
    before = (await _row(APPROVED, "sms")).body_text
    res = await c.post(
        f"{BASE}/{APPROVED}/sms/preview",
        headers=c.owner_headers,
        json={"body_text": "Approved! Password {temp_password} at {login_url}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["valid"] is True
    assert catalog.SAMPLE_VALUES["temp_password"] in body["preview"]["body_text"]
    assert body["preview"]["segment_count"] == 1
    assert (await _row(APPROVED, "sms")).body_text == before


async def test_preview_reports_an_invalid_template_as_200_with_an_error(client):
    c, _ = client
    res = await c.post(
        f"{BASE}/{APPROVED}/sms/preview", headers=c.owner_headers, json={"body_text": "No password here."}
    )
    assert res.status_code == 200
    assert res.json()["valid"] is False
    assert "{temp_password}" in res.json()["error"]
    assert res.json()["preview"] is None


# ── write ─────────────────────────────────────────────────────────────────

async def test_saving_a_template_persists_it_and_the_send_path_picks_it_up(client):
    c, _ = client
    new_text = "IpAdmin: approved! Sign in at {login_url} with {admin_email} / {temp_password}."
    res = await c.put(f"{BASE}/{APPROVED}/sms", headers=c.owner_headers, json={"body_text": new_text})
    assert res.status_code == 200, res.text
    assert res.json()["body_text"] == new_text
    assert res.json()["is_default"] is False
    assert res.json()["updated_at"] is not None

    row = await _row(APPROVED, "sms")
    assert row.body_text == new_text
    assert row.updated_by_platform_owner_id == c.owner_id

    rendered = await template_store.render_sms(APPROVED, catalog.sample_values(APPROVED))
    assert rendered.startswith("IpAdmin: approved!")
    assert catalog.SAMPLE_VALUES["temp_password"] in rendered

    await _reset(c, c.owner_headers, APPROVED, "sms")
    assert (await _row(APPROVED, "sms")).body_text == catalog.DEFAULTS[(APPROVED, "sms")].body_text


async def test_saving_an_email_template_keeps_both_parts(client):
    c, _ = client
    default = catalog.DEFAULTS[(APPROVED, "email")]
    res = await c.put(
        f"{BASE}/{APPROVED}/email",
        headers=c.owner_headers,
        json={
            "subject": "Welcome aboard, {contact_name}",
            "body_text": "Sign in as {admin_email} with {temp_password}.",
            "body_html": "<p>Sign in as {admin_email} with <code>{temp_password}</code>.</p>",
        },
    )
    assert res.status_code == 200, res.text
    subject, html, text = await template_store.render_email(APPROVED, catalog.sample_values(APPROVED))
    assert subject == "Welcome aboard, Kwame Mensah"
    assert "<code>Xk7mQ2pR9wLt</code>" in html
    assert "Sign in as admin@aflao-community.com with Xk7mQ2pR9wLt." in text

    await _reset(c, c.owner_headers, APPROVED, "email")
    assert (await _row(APPROVED, "email")).body_html == default.body_html


@pytest.mark.parametrize("payload,expected", [
    ({"body_text": "Approved, no password."}, "{temp_password}"),
    ({"body_text": "{temp_password} and {voucher_code}"}, "{voucher_code}"),
    ({"body_text": "{temp_password} and a stray {"}, "malformed"),
    ({"body_text": "{temp_password.__class__}"}, "malformed"),
    ({"body_text": "   "}, "empty"),
    ({"body_text": "{temp_password} " + "x" * 700}, "segments"),
])
async def test_an_invalid_save_is_refused_and_changes_nothing(client, payload, expected):
    c, _ = client
    before = (await _row(APPROVED, "sms")).body_text
    res = await c.put(f"{BASE}/{APPROVED}/sms", headers=c.owner_headers, json=payload)
    assert res.status_code == 400, res.text
    assert expected in res.json()["detail"]
    assert (await _row(APPROVED, "sms")).body_text == before


async def test_unknown_fields_in_the_payload_are_refused(client):
    c, _ = client
    res = await c.put(
        f"{BASE}/{APPROVED}/sms",
        headers=c.owner_headers,
        json={"body_text": "{temp_password}", "event_type": "something_else"},
    )
    assert res.status_code == 422


async def test_subject_and_html_sent_on_an_sms_template_are_dropped_not_stored(client):
    """The CHECK constraint forbids them; the route must not hand them over."""
    c, _ = client
    res = await c.put(
        f"{BASE}/{APPROVED}/sms",
        headers=c.owner_headers,
        json={"body_text": "{temp_password} at {login_url}", "subject": "nope", "body_html": "<p>nope</p>"},
    )
    assert res.status_code == 200, res.text
    row = await _row(APPROVED, "sms")
    assert row.subject is None and row.body_html is None
    await _reset(c, c.owner_headers, APPROVED, "sms")


async def test_reset_restores_the_shipped_default_and_flags_it_as_default(client):
    c, _ = client
    await c.put(f"{BASE}/trial_expired/sms", headers=c.owner_headers, json={"body_text": "Trial over. {billing_url}"})
    assert (await c.get(f"{BASE}/trial_expired/sms", headers=c.owner_headers)).json()["is_default"] is False

    res = await c.post(f"{BASE}/trial_expired/sms/reset", headers=c.owner_headers)
    assert res.status_code == 200
    assert res.json()["is_default"] is True
    assert res.json()["body_text"] == catalog.DEFAULTS[("trial_expired", "sms")].body_text


async def test_a_deleted_row_falls_back_to_the_default_instead_of_failing(client):
    c, _ = client
    async with async_session_factory() as db:
        row = await _row("account_reactivated", "sms")
        await db.delete(await db.merge(row))
        await db.commit()

    rendered = await template_store.render_sms("account_reactivated", catalog.sample_values("account_reactivated"))
    assert rendered == catalog.render_sms(
        catalog.DEFAULTS[("account_reactivated", "sms")].body_text, catalog.sample_values("account_reactivated")
    )
    body = (await c.get(f"{BASE}/account_reactivated/sms", headers=c.owner_headers)).json()
    assert body["is_default"] is True
    assert body["body_text"] == catalog.DEFAULTS[("account_reactivated", "sms")].body_text

    # Put the seeded row back so this suite leaves the tables as it found them.
    await _reset(c, c.owner_headers, "account_reactivated", "sms")
    assert await _row("account_reactivated", "sms") is not None


async def test_text_stored_outside_the_endpoint_still_cannot_break_a_send(client):
    c, _ = client
    async with async_session_factory() as db:
        row = await db.merge(await _row("trial_expired", "sms"))
        row.body_text = "Broken {billing_url"
        await db.commit()

    rendered = await template_store.render_sms("trial_expired", catalog.sample_values("trial_expired"))
    assert "Broken" not in rendered
    assert catalog.SAMPLE_VALUES["billing_url"] in rendered
    await _reset(c, c.owner_headers, "trial_expired", "sms")
