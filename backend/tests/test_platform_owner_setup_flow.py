"""Platform-owner security setup, steps 1-2 (verify phone, set security
question) and the owner side of the shared OTP store — against a REAL database.

Runs the FastAPI app in-process with SMS sends captured instead of sent, like
tests/test_platform_owner_login_flow.py. Only runs against a disposable
database:

    PLATFORM_OWNER_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("PLATFORM_OWNER_FLOW_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs PLATFORM_OWNER_FLOW_TEST_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import func, select

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import AdminOtpCode, PlatformOwner, PlatformOwnerOtpCode, PlatformOwnerSecurityEvent
    from src.modules.admin_accounts import otp as otp_service
    from src.modules.admin_accounts.security_questions import SECURITY_QUESTIONS, normalize_answer
    from src.modules.auth.tokens import admin_token_response, platform_owner_token_response
    from src.modules.platform import owner_security_routes
    from src.modules.platform import routes as platform_routes
    from src.modules.sms.types import SMSSendResult
    from src.utils.auth import hash_password, verify_password

PASSWORD = "Owner-Passw0rd"
PHONE_INPUT = "024 400 0444"
PHONE = "233244000444"
OTHER_PHONE_INPUT = "0205550666"
OTHER_PHONE = "233205550666"


class Outbox:
    def __init__(self):
        self.sent = []  # (purpose, phone, code)
        self.fail_next = False

    async def otp(self, phone, code, *, purpose):
        if self.fail_next:
            self.fail_next = False
            return SMSSendResult(success=False, error="arkesel_http_500")
        self.sent.append((purpose, phone, code))
        return SMSSendResult(success=True, provider_reference="test")

    def last_code(self):
        return self.sent[-1][2]


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    outbox = Outbox()
    monkeypatch.setattr(owner_security_routes, "send_otp_sms", outbox.otp)

    async def no_limit(key, bucket, limit=10, window_seconds=60):
        return None

    monkeypatch.setattr(owner_security_routes, "enforce_rate_limit", no_limit)
    monkeypatch.setattr(platform_routes, "enforce_rate_limit", no_limit)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as client:
        yield client, outbox


async def make_owner(**values) -> "PlatformOwner":
    async with async_session_factory() as db:
        owner = PlatformOwner(
            email=f"owner-{uuid.uuid4().hex[:10]}@throwaway.test",
            password_hash=hash_password(PASSWORD), name="Owner", is_active=True, **values,
        )
        db.add(owner)
        await db.commit()
        return owner


async def row(owner_id) -> "PlatformOwner":
    async with async_session_factory() as db:
        return await db.get(PlatformOwner, owner_id)


async def events(owner_id, event_type):
    async with async_session_factory() as db:
        return (await db.execute(
            select(PlatformOwnerSecurityEvent)
            .where(PlatformOwnerSecurityEvent.platform_owner_id == owner_id, PlatformOwnerSecurityEvent.event_type == event_type)
            .order_by(PlatformOwnerSecurityEvent.created_at)
        )).scalars().all()


def auth(owner):
    return {"Authorization": f"Bearer {platform_owner_token_response(owner).access_token}"}


async def send_code(client, owner, phone=PHONE_INPUT, password=PASSWORD):
    return await client.post("/platform/setup/phone", headers=auth(owner), json={"phone": phone, "current_password": password})


async def verify_code(client, owner, code):
    return await client.post("/platform/setup/phone/verify", headers=auth(owner), json={"code": code})


async def set_question(client, owner, key="birth_town", answer="Accra", password=PASSWORD):
    return await client.post(
        "/platform/setup/security-question", headers=auth(owner),
        json={"security_question": key, "security_answer": answer, "current_password": password},
    )


# ── Shared OTP store: owner side ──────────────────────────────────────────


async def test_owner_codes_live_in_their_own_table():
    owner = await make_owner()
    async with async_session_factory() as db:
        before_admin = (await db.execute(select(func.count()).select_from(AdminOtpCode))).scalar()
        row_, code = await otp_service.issue_code(db, platform_owner_id=owner.id, purpose="setup", phone=PHONE)
        await db.commit()
        assert isinstance(row_, PlatformOwnerOtpCode) and row_.platform_owner_id == owner.id
        assert (await db.execute(select(func.count()).select_from(AdminOtpCode))).scalar() == before_admin
    async with async_session_factory() as db:
        result = await otp_service.verify_code(db, platform_owner_id=owner.id, purpose="setup", code=code)
        await db.commit()
    assert result.ok and isinstance(result.row, PlatformOwnerOtpCode)


async def test_an_owner_code_is_invisible_to_the_admin_store():
    owner = await make_owner()
    async with async_session_factory() as db:
        _, code = await otp_service.issue_code(db, platform_owner_id=owner.id, purpose="reset", phone=PHONE)
        await db.commit()
    async with async_session_factory() as db:
        # Same UUID, same purpose name, other store: nothing to find.
        result = await otp_service.verify_code(db, admin_user_id=owner.id, purpose="reset", code=code)
    assert not result.ok and result.reason == "no_code"


async def test_owner_codes_follow_the_same_attempt_limit():
    owner = await make_owner()
    async with async_session_factory() as db:
        _, code = await otp_service.issue_code(db, platform_owner_id=owner.id, purpose="setup", phone=PHONE)
        await db.commit()
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    reasons = []
    for _ in range(otp_service.OTP_MAX_ATTEMPTS):
        async with async_session_factory() as db:
            reasons.append((await otp_service.verify_code(db, platform_owner_id=owner.id, purpose="setup", code=wrong)).reason)
    assert reasons == ["mismatch"] * 4 + ["locked"]
    async with async_session_factory() as db:
        assert (await otp_service.verify_code(db, platform_owner_id=owner.id, purpose="setup", code=code)).reason == "locked"


@pytest.mark.parametrize("kwargs,exc", [
    ({}, TypeError),  # neither account
    ({"both": True}, TypeError),
    ({"admin_purpose_on_owner": True}, ValueError),  # 'onboarding' is admin-only
    ({"owner_purpose_on_admin": True}, ValueError),  # 'setup' is owner-only
])
async def test_store_selection_rejects_ambiguous_or_mismatched_calls(kwargs, exc):
    some_id = uuid.uuid4()
    call = {"purpose": "reset", "phone": PHONE}
    if kwargs.get("both"):
        call.update(admin_user_id=some_id, platform_owner_id=some_id)
    elif kwargs.get("admin_purpose_on_owner"):
        call.update(platform_owner_id=some_id, purpose="onboarding")
    elif kwargs.get("owner_purpose_on_admin"):
        call.update(admin_user_id=some_id, purpose="setup")
    async with async_session_factory() as db:
        with pytest.raises(exc):
            await otp_service.issue_code(db, **call)
        verify_call = {k: v for k, v in call.items() if k != "phone"} | {"code": "123456"}
        with pytest.raises(exc):
            await otp_service.verify_code(db, **verify_call)


# ── Status ────────────────────────────────────────────────────────────────


async def test_status_reports_pending_setup_and_the_question_list(env):
    client, _ = env
    owner = await make_owner()
    res = await client.get("/platform/setup/status", headers=auth(owner))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["must_complete_security_setup"] is True
    assert body["phone_verified"] is False and body["phone"] is None
    assert body["has_security_question"] is False and body["has_character_code"] is False
    assert {q["key"] for q in body["security_questions"]} == set(SECURITY_QUESTIONS)


# ── Step 1: phone ─────────────────────────────────────────────────────────


async def test_phone_verification_happy_path(env):
    client, outbox = env
    owner = await make_owner()
    res = await send_code(client, owner)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "sent" and PHONE not in res.text  # masked
    purpose, phone, code = outbox.sent[-1]
    assert (purpose, phone) == ("setup", PHONE)

    res = await verify_code(client, owner, code)
    assert res.status_code == 200, res.text
    fresh = await row(owner.id)
    assert fresh.phone == PHONE and fresh.phone_verified is True
    (evt,) = await events(owner.id, "phone_verified")
    assert PHONE not in str(evt.detail), "event detail must be masked"
    assert (await client.get("/platform/setup/status", headers=auth(owner))).json()["phone_verified"] is True


async def test_sending_a_code_requires_the_current_password(env):
    client, outbox = env
    owner = await make_owner()
    before = len(outbox.sent)
    res = await send_code(client, owner, password="not-my-password")
    assert res.status_code == 400 and res.json()["detail"] == owner_security_routes.WRONG_PASSWORD
    assert len(outbox.sent) == before, "no SMS on a wrong password"
    async with async_session_factory() as db:
        assert (await db.execute(select(func.count()).select_from(PlatformOwnerOtpCode)
                                 .where(PlatformOwnerOtpCode.platform_owner_id == owner.id))).scalar() == 0


async def test_invalid_phone_is_rejected(env):
    client, _ = env
    owner = await make_owner()
    res = await send_code(client, owner, phone="12345")
    assert res.status_code == 400


async def test_wrong_code_does_not_verify(env):
    client, outbox = env
    owner = await make_owner()
    await send_code(client, owner)
    code = outbox.last_code()
    res = await verify_code(client, owner, f"{(int(code) + 1) % 1_000_000:06d}")
    assert res.status_code == 400 and res.json()["detail"].startswith("Incorrect code")
    assert (await row(owner.id)).phone_verified is False


async def test_verify_without_a_code_says_request_one(env):
    client, _ = env
    owner = await make_owner()
    res = await verify_code(client, owner, "123456")
    assert res.status_code == 400 and "Request a new code" in res.json()["detail"]


async def test_failed_sms_leaves_no_live_code(env):
    client, outbox = env
    owner = await make_owner()
    outbox.fail_next = True
    res = await send_code(client, owner)
    assert res.status_code == 502
    async with async_session_factory() as db:
        open_codes = (await db.execute(select(func.count()).select_from(PlatformOwnerOtpCode).where(
            PlatformOwnerOtpCode.platform_owner_id == owner.id, PlatformOwnerOtpCode.consumed_at.is_(None)))).scalar()
    assert open_codes == 0


async def test_phone_step_is_repeatable_and_the_latest_verified_number_wins(env):
    client, outbox = env
    owner = await make_owner()
    await send_code(client, owner)
    assert (await verify_code(client, owner, outbox.last_code())).status_code == 200
    await send_code(client, owner, phone=OTHER_PHONE_INPUT)
    assert (await verify_code(client, owner, outbox.last_code())).status_code == 200
    assert (await row(owner.id)).phone == OTHER_PHONE
    evts = await events(owner.id, "phone_verified")
    assert len(evts) == 2 and evts[1].detail["replaced"] is not None


# ── Step 2: security question ─────────────────────────────────────────────


async def test_security_question_is_saved_hashed_and_normalized(env):
    client, _ = env
    owner = await make_owner(security_answer_attempt_count=2)
    res = await set_question(client, owner, "birth_town", "  Accra  ")
    assert res.status_code == 200, res.text
    fresh = await row(owner.id)
    assert fresh.security_question == "birth_town"
    assert "accra" not in fresh.security_answer_hash.lower()
    assert verify_password(normalize_answer("ACCRA"), fresh.security_answer_hash)
    assert fresh.security_answer_attempt_count == 0
    (evt,) = await events(owner.id, "security_question_set")
    assert "ccra" not in str(evt.detail).lower(), "the answer must never reach the event"


async def test_security_question_requires_the_current_password(env):
    client, _ = env
    owner = await make_owner()
    res = await set_question(client, owner, password="not-my-password")
    assert res.status_code == 400 and res.json()["detail"] == owner_security_routes.WRONG_PASSWORD
    assert (await row(owner.id)).security_answer_hash is None


@pytest.mark.parametrize("key,answer", [("not_a_question", "Accra"), ("birth_town", " a "), ("birth_town", "")])
async def test_security_question_validation(env, key, answer):
    client, _ = env
    owner = await make_owner()
    assert (await set_question(client, owner, key, answer)).status_code == 400
    assert (await row(owner.id)).security_answer_hash is None


async def test_security_question_can_be_replaced_during_setup(env):
    client, _ = env
    owner = await make_owner()
    await set_question(client, owner, "birth_town", "Accra")
    assert (await set_question(client, owner, "first_school", "Achimota")).status_code == 200
    fresh = await row(owner.id)
    assert fresh.security_question == "first_school"
    assert verify_password("achimota", fresh.security_answer_hash)
    assert [e.detail["replaced"] for e in await events(owner.id, "security_question_set")] == [False, True]


# ── Scope: setup-only, owner-only, strict bodies ──────────────────────────


async def test_steps_refuse_once_setup_is_complete(env):
    client, outbox = env
    owner = await make_owner(must_complete_security_setup=False, phone=PHONE, phone_verified=True)
    before = len(outbox.sent)
    for res in (
        await send_code(client, owner, phone=OTHER_PHONE_INPUT),
        await verify_code(client, owner, "123456"),
        await set_question(client, owner),
    ):
        assert res.status_code == 409, res.text
    assert len(outbox.sent) == before
    fresh = await row(owner.id)
    assert fresh.phone == PHONE and fresh.security_answer_hash is None
    assert (await client.get("/platform/setup/status", headers=auth(owner))).status_code == 200


async def test_setup_endpoints_need_an_owner_token(env):
    client, _ = env
    from src.db.models import AdminUser, ISPOperator
    slug = f"own-{uuid.uuid4().hex[:8]}"
    async with async_session_factory() as db:
        op = ISPOperator(name=slug, slug=slug, contact_email=f"{slug}@throwaway.test", status="approved")
        db.add(op)
        await db.flush()
        admin = AdminUser(isp_operator_id=op.id, email=f"{slug}@throwaway.test", password_hash=hash_password(PASSWORD),
                          role="superadmin", is_active=True)
        db.add(admin)
        await db.commit()
    admin_headers = {"Authorization": f"Bearer {admin_token_response(admin).access_token}"}
    for method, path in (("GET", "/platform/setup/status"), ("POST", "/platform/setup/phone"),
                         ("POST", "/platform/setup/phone/verify"), ("POST", "/platform/setup/security-question")):
        assert (await client.request(method, path)).status_code in (401, 403), f"{path} without a token"
        assert (await client.request(method, path, headers=admin_headers, json={})).status_code == 401, f"{path} with an admin token"


async def test_unknown_fields_are_rejected(env):
    client, _ = env
    owner = await make_owner()
    res = await client.post("/platform/setup/phone", headers=auth(owner),
                            json={"phone": PHONE_INPUT, "current_password": PASSWORD, "phone_verified": True})
    assert res.status_code == 422


# ── Step 3/4: generate, then confirm the code ─────────────────────────────


async def generate(client, owner, password=PASSWORD):
    res = await client.post("/platform/setup/character-code", headers=auth(owner), json={"current_password": password})
    assert res.status_code == 200, res.text
    return res.json()["character_code"]


async def check_positions(client, owner):
    return await client.get("/platform/setup/character-code/check", headers=auth(owner))


async def check(client, owner, characters):
    return await client.post("/platform/setup/character-code/check", headers=auth(owner), json={"characters": characters})


def answer_for(code, positions, *, wrong=False):
    chars = {str(p): code[p - 1] for p in positions}
    if wrong:
        last = str(positions[-1])
        chars[last] = next(c for c in "ABCDEFGHJKMNPQRSTUVWXYZ23456789" if c != chars[last])
    return chars


async def test_confirm_needs_a_code_first(env):
    client, _ = env
    owner = await make_owner()
    assert (await check_positions(client, owner)).status_code == 409


async def test_confirm_positions_are_fixed_until_answered_and_misses_are_not_login_failures(env):
    client, _ = env
    owner = await make_owner()
    code = await generate(client, owner)
    first = (await check_positions(client, owner)).json()["positions"]
    assert (await check_positions(client, owner)).json()["positions"] == first, "reloading must not redeal"
    assert len(first) == 3

    res = await check(client, owner, answer_for(code, first, wrong=True))
    assert res.status_code == 400
    assert (await check_positions(client, owner)).json()["positions"] == first
    fresh = await row(owner.id)
    assert fresh.challenge_attempt_count == 0 and fresh.challenge_locked_until is None

    res = await check(client, owner, answer_for(code, first))
    assert res.status_code == 200, res.text
    status_body = (await client.get("/platform/setup/status", headers=auth(owner))).json()
    assert status_body["character_code_confirmed"] is True
    assert (await row(owner.id)).challenge_pending_positions is None


async def test_confirm_rejects_other_positions(env):
    client, _ = env
    owner = await make_owner()
    code = await generate(client, owner)
    asked = (await check_positions(client, owner)).json()["positions"]
    others = [p for p in range(1, 13) if p not in asked][:3]
    assert (await check(client, owner, answer_for(code, others))).status_code == 400
    assert (await client.get("/platform/setup/status", headers=auth(owner))).json()["character_code_confirmed"] is False


async def test_regenerating_the_code_unconfirms_it(env):
    client, _ = env
    owner = await make_owner()
    code = await generate(client, owner)
    asked = (await check_positions(client, owner)).json()["positions"]
    assert (await check(client, owner, answer_for(code, asked))).status_code == 200
    await generate(client, owner)
    body = (await client.get("/platform/setup/status", headers=auth(owner))).json()
    assert body["has_character_code"] is True and body["character_code_confirmed"] is False


# ── Completion: all or nothing ────────────────────────────────────────────


async def complete_all_steps(client, outbox, owner):
    """Drive steps 1-4 through the API. Returns the plaintext code."""
    await send_code(client, owner)
    assert (await verify_code(client, owner, outbox.last_code())).status_code == 200
    assert (await set_question(client, owner)).status_code == 200
    code = await generate(client, owner)
    asked = (await check_positions(client, owner)).json()["positions"]
    assert (await check(client, owner, answer_for(code, asked))).status_code == 200
    return code


async def complete(client, owner, password=PASSWORD):
    return await client.post("/platform/setup/complete", headers=auth(owner), json={"current_password": password})


@pytest.mark.parametrize("skip", ["phone", "security_question", "character_code", "character_code_confirmation"])
async def test_completion_refuses_when_any_single_step_is_missing(env, skip):
    client, outbox = env
    owner = await make_owner()
    if skip != "phone":
        await send_code(client, owner)
        await verify_code(client, owner, outbox.last_code())
    if skip != "security_question":
        await set_question(client, owner)
    if skip != "character_code":
        code = await generate(client, owner)
        if skip != "character_code_confirmation":
            asked = (await check_positions(client, owner)).json()["positions"]
            await check(client, owner, answer_for(code, asked))

    res = await complete(client, owner)
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["missing"] == [skip]
    fresh = await row(owner.id)
    assert fresh.must_complete_security_setup is True and fresh.token_version == 0
    assert not await events(owner.id, "setup_completed")


async def test_completion_requires_the_password(env):
    client, outbox = env
    owner = await make_owner()
    await complete_all_steps(client, outbox, owner)
    res = await complete(client, owner, password="not-my-password")
    assert res.status_code == 400
    assert (await row(owner.id)).must_complete_security_setup is True


async def test_full_setup_then_first_challenge_sign_in(env):
    client, outbox = env
    owner = await make_owner()
    old_headers = auth(owner)
    assert (await client.get("/platform/me", headers=old_headers)).status_code == 403  # gated while pending

    code = await complete_all_steps(client, outbox, owner)
    assert (await client.get("/platform/setup/status", headers=old_headers)).json()["ready_to_complete"] is True
    res = await complete(client, owner)
    assert res.status_code == 200 and res.json() == {"status": "complete", "logout": True}

    fresh = await row(owner.id)
    assert fresh.must_complete_security_setup is False and fresh.token_version == 1
    assert fresh.challenge_pending_positions is None and fresh.challenge_pending_jti is None
    (evt,) = await events(owner.id, "setup_completed")
    assert PHONE not in str(evt.detail)
    # Every earlier session is dead, including the one that finished setup.
    assert (await client.get("/platform/setup/status", headers=old_headers)).status_code == 401

    # The next sign-in is the first with the challenge, and the code shown
    # during setup answers it.
    login = await client.post("/platform/auth/login", json={"email": owner.email, "password": PASSWORD})
    assert login.status_code == 200 and login.json()["challenge_required"] is True
    body = login.json()
    res = await client.post("/platform/auth/challenge", json={
        "challenge_token": body["challenge_token"], "characters": answer_for(code, body["positions"])})
    assert res.status_code == 200, res.text
    headers = {"Authorization": f"Bearer {res.json()['access_token']}"}
    assert (await client.get("/platform/me", headers=headers)).status_code == 200  # gate open
    # ...and setup can't be re-run from this session.
    assert (await client.post("/platform/setup/complete", headers=headers, json={"current_password": PASSWORD})).status_code == 409
    assert (await client.post("/platform/setup/character-code", headers=headers, json={"current_password": PASSWORD})).status_code == 409
