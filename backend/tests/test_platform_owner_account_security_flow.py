"""Changing the platform owner's phone and security question after setup
(POST /platform/me/phone, /phone/verify, /security-question) and the Security
details on GET /platform/me — against a REAL database, SMS captured.

Plus static checks on the Settings page's Security block (no database).

The database part only runs against a disposable database:

    PLATFORM_OWNER_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("PLATFORM_OWNER_FLOW_TEST_DATABASE_URL", "")
DB_OK = bool(FLOW_DB) and "throwaway" in FLOW_DB.rsplit("/", 1)[-1] and os.getenv("DATABASE_URL") == FLOW_DB
needs_db = pytest.mark.skipif(not DB_OK, reason="needs PLATFORM_OWNER_FLOW_TEST_DATABASE_URL (a throwaway database) == DATABASE_URL")
pytestmark = pytest.mark.asyncio(loop_scope="module")

if DB_OK:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import func, select

    from platform_owner_session import create_ready_owner
    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import PlatformOwner, PlatformOwnerOtpCode, PlatformOwnerSecurityEvent
    from src.modules.admin_accounts.security_questions import SECURITY_QUESTIONS, normalize_answer
    from src.modules.auth.tokens import platform_owner_token_response
    from src.modules.platform import owner_account_routes
    from src.modules.sms.types import SMSSendResult
    from src.utils.auth import hash_password, verify_password

PASSWORD = "Owner-Passw0rd"
OLD_PHONE = "233244000601"
NEW_PHONE_INPUT, NEW_PHONE = "020 555 0602", "233205550602"
SETTINGS = Path(__file__).resolve().parents[1] / "src" / "platform_portal" / "settings.html"


class Outbox:
    def __init__(self):
        self.sent = []  # (kind, phone, extra)
        self.fail_next_otp = False

    async def otp(self, phone, code, *, purpose):
        if self.fail_next_otp:
            self.fail_next_otp = False
            return SMSSendResult(success=False, error="arkesel_http_500")
        self.sent.append((f"otp_{purpose}", phone, code))
        return SMSSendResult(success=True, provider_reference="test")

    async def phone_changed(self, old_phone, new_masked):
        self.sent.append(("phone_changed", old_phone, new_masked))
        return SMSSendResult(success=True, provider_reference="test")

    async def question_changed(self, phone):
        self.sent.append(("question_changed", phone, None))
        return SMSSendResult(success=True, provider_reference="test")

    def last(self, kind):
        return next(item for item in reversed(self.sent) if item[0] == kind)

    def count(self, kind):
        return sum(1 for item in self.sent if item[0] == kind)


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    outbox = Outbox()
    monkeypatch.setattr(owner_account_routes, "send_otp_sms", outbox.otp)
    monkeypatch.setattr(owner_account_routes, "send_platform_owner_phone_changed_sms", outbox.phone_changed)
    monkeypatch.setattr(owner_account_routes, "send_platform_owner_question_changed_sms", outbox.question_changed)

    async def no_limit(*a, **k):
        return None
    monkeypatch.setattr(owner_account_routes, "enforce_rate_limit", no_limit)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver/api/v1") as client:
        yield client, outbox


async def ready_owner(**values):
    email = f"acct-{uuid.uuid4().hex[:10]}@ci.test"
    await create_ready_owner(email, PASSWORD, phone=values.pop("phone", OLD_PHONE),
                             security_question="birth_town", security_answer="Accra", **values)
    return await owner_by_email(email)


async def owner_by_email(email):
    async with async_session_factory() as db:
        return (await db.execute(select(PlatformOwner).where(PlatformOwner.email == email))).scalar_one()


async def row(owner_id):
    async with async_session_factory() as db:
        return await db.get(PlatformOwner, owner_id)


async def events(owner_id, event_type):
    async with async_session_factory() as db:
        return (await db.execute(select(PlatformOwnerSecurityEvent).where(
            PlatformOwnerSecurityEvent.platform_owner_id == owner_id,
            PlatformOwnerSecurityEvent.event_type == event_type))).scalars().all()


async def open_codes(owner_id):
    async with async_session_factory() as db:
        return (await db.execute(select(func.count()).select_from(PlatformOwnerOtpCode).where(
            PlatformOwnerOtpCode.platform_owner_id == owner_id, PlatformOwnerOtpCode.consumed_at.is_(None)))).scalar()


def auth(owner):
    return {"Authorization": f"Bearer {platform_owner_token_response(owner).access_token}"}


async def send(client, owner, phone=NEW_PHONE_INPUT, password=PASSWORD):
    return await client.post("/platform/me/phone", headers=auth(owner), json={"phone": phone, "current_password": password})


async def verify(client, owner, code):
    return await client.post("/platform/me/phone/verify", headers=auth(owner), json={"code": code})


async def change_question(client, owner, key="first_school", answer="Achimota", password=PASSWORD):
    return await client.post("/platform/me/security-question", headers=auth(owner),
                             json={"security_question": key, "security_answer": answer, "current_password": password})


# ── GET /platform/me ──────────────────────────────────────────────────────


@needs_db
async def test_me_shows_masked_phone_and_question_but_never_the_answer(env):
    client, _ = env
    owner = await ready_owner()
    body = (await client.get("/platform/me", headers=auth(owner))).json()
    assert body["phone_verified"] is True and body["phone"] and OLD_PHONE not in body["phone"]
    assert body["phone"].endswith(OLD_PHONE[-3:])
    assert body["security_question"] == "birth_town"
    assert body["security_question_text"] == SECURITY_QUESTIONS["birth_town"]
    assert {q["key"] for q in body["security_questions"]} == set(SECURITY_QUESTIONS)
    assert "accra" not in str(body).lower() and "hash" not in str(body).lower()


# ── Phone change ──────────────────────────────────────────────────────────


@needs_db
async def test_phone_change_happy_path_texts_the_old_number(env):
    client, outbox = env
    owner = await ready_owner()
    res = await send(client, owner)
    assert res.status_code == 200, res.text
    assert NEW_PHONE not in res.text, "the response must mask the number"
    kind, phone, code = outbox.last("otp_phone_change")
    assert phone == NEW_PHONE
    assert (await row(owner.id)).phone == OLD_PHONE, "nothing changes until the code comes back"

    before = outbox.count("phone_changed")
    res = await verify(client, owner, code)
    assert res.status_code == 200, res.text
    fresh = await row(owner.id)
    assert fresh.phone == NEW_PHONE and fresh.phone_verified is True
    assert outbox.count("phone_changed") == before + 1
    _, told, new_masked = outbox.last("phone_changed")
    assert told == OLD_PHONE, "the OLD number is the one told"
    assert NEW_PHONE not in new_masked and new_masked.endswith(NEW_PHONE[-3:])
    (evt,) = await events(owner.id, "phone_changed")
    assert OLD_PHONE not in str(evt.detail) and NEW_PHONE not in str(evt.detail), "event detail is masked"
    assert evt.sms_sent is True and evt.sms_error is None, "the alert outcome is recorded"
    assert (await client.get("/platform/me", headers=auth(owner))).json()["phone"].endswith(NEW_PHONE[-3:])


@needs_db
async def test_phone_change_requires_the_current_password(env):
    client, outbox = env
    owner = await ready_owner()
    before = len(outbox.sent)
    res = await send(client, owner, password="not-my-password")
    assert res.status_code == 400 and res.json()["detail"] == owner_account_routes.WRONG_PASSWORD
    assert len(outbox.sent) == before and await open_codes(owner.id) == 0


@needs_db
@pytest.mark.parametrize("phone,why", [("12345", "invalid"), (OLD_PHONE, "same as current")])
async def test_phone_change_rejects_bad_or_unchanged_numbers(env, phone, why):
    client, outbox = env
    owner = await ready_owner()
    before = len(outbox.sent)
    assert (await send(client, owner, phone=phone)).status_code == 400, why
    assert len(outbox.sent) == before


@needs_db
async def test_wrong_code_changes_nothing(env):
    client, outbox = env
    owner = await ready_owner()
    await send(client, owner)
    code = outbox.last("otp_phone_change")[2]
    res = await verify(client, owner, f"{(int(code) + 1) % 1_000_000:06d}")
    assert res.status_code == 400 and res.json()["detail"].startswith("Incorrect code")
    assert (await row(owner.id)).phone == OLD_PHONE
    assert await events(owner.id, "phone_changed") == []


@needs_db
async def test_verify_without_a_code_asks_for_one(env):
    client, _ = env
    owner = await ready_owner()
    res = await verify(client, owner, "123456")
    assert res.status_code == 400 and "Request a new code" in res.json()["detail"]


@needs_db
async def test_failed_sms_leaves_no_live_code(env):
    client, outbox = env
    owner = await ready_owner()
    outbox.fail_next_otp = True
    assert (await send(client, owner)).status_code == 502
    assert await open_codes(owner.id) == 0


@needs_db
async def test_setup_codes_and_phone_change_codes_do_not_mix(env):
    """A code issued for setup must not verify a phone change (separate
    purposes in the shared OTP store)."""
    client, _ = env
    owner = await ready_owner()
    from src.modules.admin_accounts import otp as otp_service
    async with async_session_factory() as db:
        _, setup_code = await otp_service.issue_code(db, platform_owner_id=owner.id, purpose="setup", phone=NEW_PHONE)
        await db.commit()
    res = await verify(client, owner, setup_code)
    assert res.status_code == 400
    assert (await row(owner.id)).phone == OLD_PHONE


# ── Security question change ──────────────────────────────────────────────


@needs_db
async def test_question_change_saves_hashed_and_texts_the_verified_phone(env):
    client, outbox = env
    owner = await ready_owner(security_answer_attempt_count=2)
    before = outbox.count("question_changed")
    res = await change_question(client, owner, "first_school", "  ACHIMOTA ")
    assert res.status_code == 200, res.text
    fresh = await row(owner.id)
    assert fresh.security_question == "first_school"
    assert verify_password(normalize_answer("achimota"), fresh.security_answer_hash)
    assert not verify_password(normalize_answer("accra"), fresh.security_answer_hash)
    assert fresh.security_answer_attempt_count == 0
    assert outbox.count("question_changed") == before + 1 and outbox.last("question_changed")[1] == OLD_PHONE
    (evt,) = await events(owner.id, "security_question_changed")
    assert "chimota" not in str(evt.detail).lower(), "the answer never reaches the event"
    assert evt.sms_sent is True


@needs_db
async def test_question_change_requires_the_current_password(env):
    client, outbox = env
    owner = await ready_owner()
    before_hash = (await row(owner.id)).security_answer_hash
    res = await change_question(client, owner, password="not-my-password")
    assert res.status_code == 400 and res.json()["detail"] == owner_account_routes.WRONG_PASSWORD
    assert (await row(owner.id)).security_answer_hash == before_hash


@needs_db
@pytest.mark.parametrize("key,answer", [("not_a_question", "Achimota"), ("first_school", " a "), ("first_school", "")])
async def test_question_change_validation(env, key, answer):
    client, _ = env
    owner = await ready_owner()
    before = (await row(owner.id)).security_answer_hash
    assert (await change_question(client, owner, key, answer)).status_code == 400
    assert (await row(owner.id)).security_answer_hash == before


# ── Only after setup: the gate ────────────────────────────────────────────


@needs_db
async def test_all_three_are_refused_while_setup_is_pending(env):
    """The inverse of the setup routes: the existing gate answers first."""
    client, outbox = env
    async with async_session_factory() as db:
        owner = PlatformOwner(email=f"pending-{uuid.uuid4().hex[:8]}@ci.test", password_hash=hash_password(PASSWORD),
                              name="Pending", is_active=True, must_complete_security_setup=True)
        db.add(owner)
        await db.commit()
    before = len(outbox.sent)
    for res in (await send(client, owner), await verify(client, owner, "123456"), await change_question(client, owner)):
        assert res.status_code == 403 and res.headers.get("X-Security-Setup-Required") == "1", res.text
    assert len(outbox.sent) == before


@needs_db
def test_the_new_routes_carry_the_setup_gate_in_the_route_table():
    """What the Phase 5 route walk will see: these are gated routes."""
    from fastapi.routing import APIRoute

    from src.middleware.auth import get_platform_owner_context

    def calls(dependant, seen):
        for d in dependant.dependencies:
            seen.add(d.call)
            calls(d, seen)
        return seen

    wanted = {("POST", "/api/v1/platform/me/phone"), ("POST", "/api/v1/platform/me/phone/verify"),
              ("POST", "/api/v1/platform/me/security-question"), ("GET", "/api/v1/platform/me")}
    found = {}
    for r in app.routes:
        if isinstance(r, APIRoute):
            for m in r.methods:
                if (m, r.path) in wanted:
                    found[(m, r.path)] = get_platform_owner_context in calls(r.dependant, set())
    assert found == {w: True for w in wanted}


# ── Settings page: the Security block (static, no database) ───────────────


def _security_block_and_modals(html: str) -> str:
    start = html.index('<div class="subhead">Security</div>')
    end = html.index("</div>", html.index('id="sec-body"')) + len("</div>")
    m1 = html.index('<div class="modal-backdrop" id="phone-modal">')
    m2 = html.index('<div class="modal-backdrop" id="question-modal">')
    m2_end = html.index("</div>\n</div>", m2) + len("</div>\n</div>")
    return html[start:end] + html[m1:m2_end]


def test_settings_security_block_uses_shared_components_only():
    html = SETTINGS.read_text()
    new = _security_block_and_modals(html)
    assert "style=" not in new, "no inline styles in the new markup"
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", new), "tokens only, no hex colours"
    for cls in ("subhead", "help", "empty", "modal-backdrop", "modal-dialog", "modal-title", "modal-actions"):
        assert f'class="{cls}' in new or f' {cls}"' in new or f" {cls} " in new, cls


def test_settings_toast_is_the_shared_component():
    html = SETTINGS.read_text()
    assert '<div class="toast" id="toast" role="status"></div>' in html
    assert "#toast{" not in html and "#toast.show" not in html, "page-local toast styles removed"


def test_settings_calls_the_new_endpoints():
    html = SETTINGS.read_text()
    for path in ("/api/v1/platform/me/phone", "/api/v1/platform/me/phone/verify", "/api/v1/platform/me/security-question"):
        assert path in html, path
