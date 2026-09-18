"""End-to-end admin provisioning / onboarding / reset flows against a REAL database.

Runs the FastAPI app in-process (no server) with SMS sends captured instead of
sent. It writes operators, admins and codes, so it only runs when explicitly
pointed at a disposable database:

    ADMIN_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>

and skips otherwise — including in the production container, whose
DATABASE_URL is the live database.
"""
from __future__ import annotations

import os
import re
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

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import select

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import AdminOtpCode, AdminPasswordResetEvent, AdminUser, PlatformOwner
    from src.modules.admin_accounts import notifications as account_notifications
    from src.modules.admin_accounts import routes as account_routes
    from src.modules.applications import service as application_service
    from src.modules.platform import routes as platform_routes
    from src.modules.sms.types import SMSSendResult
    from src.utils.auth import hash_password

OWNER_PASSWORD = "Owner-Passw0rd"
NEW_PASSWORD = "Str0ngerPass"
NEWER_PASSWORD = "Ev3nStronger"


class SmsOutbox:
    def __init__(self):
        self.sent = []  # (kind, phone, text)
        self.fail_next = False

    async def temp_password(self, admin, temp_password, *, reason="new"):
        kind = "temp" if reason == "new" else f"temp_{reason}"
        self.sent.append((kind, admin.phone, temp_password))
        return SMSSendResult(success=True, provider_reference="test")

    async def otp(self, phone, code, *, purpose):
        if self.fail_next:
            self.fail_next = False
            return SMSSendResult(success=False, error="arkesel_http_500")
        self.sent.append((purpose, phone, code))
        return SMSSendResult(success=True, provider_reference="test")

    def last(self, kind):
        return next(item for item in reversed(self.sent) if item[0] == kind)


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    outbox = SmsOutbox()
    monkeypatch.setattr(platform_routes, "send_temp_password_sms", outbox.temp_password)
    monkeypatch.setattr(application_service, "send_temp_password_sms", outbox.temp_password)
    monkeypatch.setattr(account_routes, "send_otp_sms", outbox.otp)
    monkeypatch.setattr(account_notifications, "send_temp_password_sms", outbox.temp_password)
    limiter_calls = []

    async def record_limit(key, bucket, limit=10, window_seconds=60):
        limiter_calls.append((key, bucket, limit, window_seconds))

    monkeypatch.setattr(platform_routes, "enforce_rate_limit", record_limit)
    outbox.limiter_calls = limiter_calls

    owner_email = f"owner-{uuid.uuid4().hex[:8]}@throwaway.test"
    async with async_session_factory() as db:
        db.add(PlatformOwner(email=owner_email, password_hash=hash_password(OWNER_PASSWORD), name="Owner", is_active=True))
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as client:
        res = await client.post("/platform/auth/login", json={"email": owner_email, "password": OWNER_PASSWORD})
        assert res.status_code == 200, res.text
        client.owner_headers = {"Authorization": f"Bearer {res.json()['access_token']}"}
        async with async_session_factory() as db:
            client.owner_id = (await db.execute(select(PlatformOwner.id).where(PlatformOwner.email == owner_email))).scalar_one()
        yield client, outbox


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def admin_row(email):
    async with async_session_factory() as db:
        return (await db.execute(select(AdminUser).where(AdminUser.email == email))).scalar_one()


async def create_operator(client, *, admin_email, phone="024 400 0001"):
    slug = f"flow-{uuid.uuid4().hex[:10]}"
    return await client.post(
        "/platform/operators",
        headers=client.owner_headers,
        json={
            "name": f"Flow {slug}",
            "slug": slug,
            "contact_email": f"{slug}@throwaway.test",
            "initial_admin_email": admin_email,
            "initial_admin_phone": phone,
            "trial_days": 14,
        },
    )


async def login(client, email, password):
    return await client.post("/auth/login", json={"email": email, "password": password})


async def onboard(client, outbox, email, temp_password, *, phone="0244000009", question="birth_town", answer="Accra"):
    token = (await login(client, email, temp_password)).json()["access_token"]
    res = await client.post("/auth/onboarding/phone", headers=bearer(token), json={"phone": phone})
    assert res.status_code == 200, res.text
    code = outbox.last("onboarding")[2]
    res = await client.post("/auth/onboarding/otp/verify", headers=bearer(token), json={"code": code})
    assert res.status_code == 200, res.text
    res = await client.post(
        "/auth/onboarding/password",
        headers=bearer(token),
        json={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD,
              "security_question": question, "security_answer": answer},
    )
    assert res.status_code == 200, res.text
    return token


# ── Phase 1: provisioning paths ───────────────────────────────────────────


async def test_path2_create_operator_generates_and_sends_temp_password(env):
    client, outbox = env
    email = f"Admin-{uuid.uuid4().hex[:6]}@Throwaway.test"
    res = await create_operator(client, admin_email=email)
    assert res.status_code == 201, res.text
    admin = res.json()["initial_admin"]
    assert admin["email"] == email.lower()
    assert admin["temp_password_sms_sent"] is True
    assert admin["phone"] == "+233 •••• ••• 001"
    assert outbox.last("temp") == ("temp", "233244000001", admin["temp_password"])

    row = await admin_row(email.lower())
    assert (row.must_complete_onboarding, row.phone_verified, row.token_version, row.role) == (True, False, 0, "superadmin")

    # Case-insensitive duplicate check.
    dup = await create_operator(client, admin_email=email.upper())
    assert dup.status_code == 409


async def test_path2_requires_a_valid_phone_and_ignores_passwords(env):
    client, _ = env
    res = await create_operator(client, admin_email=f"x-{uuid.uuid4().hex[:6]}@throwaway.test", phone="12345")
    assert res.status_code == 422
    slug = f"flow-{uuid.uuid4().hex[:10]}"
    res = await client.post(
        "/platform/operators",
        headers=client.owner_headers,
        json={"name": "No phone", "slug": slug, "contact_email": f"{slug}@throwaway.test",
              "initial_admin_email": f"{slug}@throwaway.test", "initial_admin_password": "Whatever123"},
    )
    assert res.status_code == 422  # initial_admin_phone missing


async def test_path3_add_admin(env):
    client, outbox = env
    created = (await create_operator(client, admin_email=f"p3-{uuid.uuid4().hex[:6]}@throwaway.test")).json()
    email = f"extra-{uuid.uuid4().hex[:6]}@throwaway.test"
    res = await client.post(
        f"/platform/operators/{created['id']}/admins",
        headers=client.owner_headers,
        json={"email": email, "phone": "0555000002", "role": "viewer"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["role"] == "viewer" and body["temp_password_sms_sent"] is True and body["temp_password"]
    assert "password" not in res.request.content.decode()
    row = await admin_row(email)
    assert row.must_complete_onboarding and row.phone == "233555000002"

    res = await client.post(
        f"/platform/operators/{created['id']}/admins",
        headers=client.owner_headers,
        json={"email": f"nophone-{uuid.uuid4().hex[:6]}@throwaway.test", "password": "Whatever123"},
    )
    assert res.status_code == 422


async def test_path1_application_approval(env):
    client, outbox = env
    email = f"applicant-{uuid.uuid4().hex[:6]}@throwaway.test"
    res = await client.post(
        "/public/apply",
        json={"isp_name": f"Applicant {uuid.uuid4().hex[:6]}", "contact_name": "Ama", "email": email,
              "phone": "0204000003", "region": "Greater Accra", "expected_sites": 1,
              "message": "We run a small hotspot network in Accra."},
    )
    assert res.status_code == 201, res.text
    app_id = res.json()["id"]
    res = await client.put(f"/platform/applications/{app_id}/approve", headers=client.owner_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["temp_password_sms_sent"] is True
    assert outbox.last("temp") == ("temp", "233204000003", body["temp_password"])
    row = await admin_row(email)
    assert row.must_complete_onboarding and not row.phone_verified and row.phone == "233204000003"


# ── Phase 2: onboarding ───────────────────────────────────────────────────


async def test_onboarding_gate_and_full_flow(env):
    client, outbox = env
    email = f"onb-{uuid.uuid4().hex[:6]}@throwaway.test"
    temp = (await create_operator(client, admin_email=email)).json()["initial_admin"]["temp_password"]

    res = await login(client, email.upper(), temp)  # case-insensitive login
    assert res.status_code == 200 and res.json()["must_complete_onboarding"] is True
    token, refresh = res.json()["access_token"], res.json()["refresh_token"]

    gated = await client.get("/sessions", headers=bearer(token))
    assert gated.status_code == 403 and gated.headers.get("x-onboarding-required") == "1"

    status_ = (await client.get("/auth/onboarding/status", headers=bearer(token))).json()
    assert status_["must_complete_onboarding"] and not status_["phone_verified"]
    assert status_["phone_on_file"] == "+233 •••• ••• 001"

    res = await client.post("/auth/onboarding/password", headers=bearer(token),
                            json={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD,
                                  "security_question": "birth_town", "security_answer": "Accra"})
    assert res.status_code == 409  # phone first

    # A failed SMS leaves no live code behind.
    outbox.fail_next = True
    res = await client.post("/auth/onboarding/phone", headers=bearer(token), json={"phone": "0244000009"})
    assert res.status_code == 502
    async with async_session_factory() as db:
        open_codes = (await db.execute(select(AdminOtpCode).where(
            AdminOtpCode.admin_user_id == (await admin_row(email)).id, AdminOtpCode.consumed_at.is_(None)))).scalars().all()
    assert open_codes == []

    # Five wrong guesses kill the code even with the right one afterwards.
    res = await client.post("/auth/onboarding/phone", headers=bearer(token), json={"phone": "0244000009"})
    assert res.status_code == 200 and res.json()["phone"] == "+233 •••• ••• 009"
    good = outbox.last("onboarding")[2]
    wrong = "000000" if good != "000000" else "111111"
    messages = [
        (await client.post("/auth/onboarding/otp/verify", headers=bearer(token), json={"code": wrong})).json()["detail"]
        for _ in range(5)
    ]
    assert messages[0].startswith("Incorrect code") and messages[-1].startswith("Too many")
    res = await client.post("/auth/onboarding/otp/verify", headers=bearer(token), json={"code": good})
    assert res.status_code == 400 and res.json()["detail"].startswith("Too many")

    # A fresh code works; the verified number (not the one typed at creation) is stored.
    await client.post("/auth/onboarding/phone", headers=bearer(token), json={"phone": "0244000009"})
    res = await client.post("/auth/onboarding/otp/verify", headers=bearer(token),
                            json={"code": outbox.last("onboarding")[2]})
    assert res.status_code == 200
    row = await admin_row(email)
    assert row.phone_verified and row.phone == "233244000009"

    for bad, expect in [
        ({"new_password": "weakpass", "confirm_password": "weakpass"}, "at least 8"),
        ({"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD + "x"}, "don't match"),
        ({"new_password": temp, "confirm_password": temp}, None),
    ]:
        res = await client.post("/auth/onboarding/password", headers=bearer(token),
                                json={**bad, "security_question": "birth_town", "security_answer": "Accra"})
        assert res.status_code == 400
        if expect:
            assert expect in res.json()["detail"]
    res = await client.post("/auth/onboarding/password", headers=bearer(token),
                            json={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD,
                                  "security_question": "not_a_question", "security_answer": "Accra"})
    assert res.status_code == 400

    res = await client.post("/auth/onboarding/password", headers=bearer(token),
                            json={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD,
                                  "security_question": "birth_town", "security_answer": "  ACCRA "})
    assert res.status_code == 200 and res.json() == {"status": "complete", "logout": True}

    # Temp-password session and refresh token are dead; temp password too.
    assert (await client.get("/auth/onboarding/status", headers=bearer(token))).status_code == 401
    assert (await client.post("/auth/refresh", json={"refresh_token": refresh})).status_code == 401
    assert (await login(client, email, temp)).status_code == 401

    res = await login(client, email, NEW_PASSWORD)
    assert res.status_code == 200 and res.json()["must_complete_onboarding"] is False
    new_token = res.json()["access_token"]
    assert (await client.get("/sessions", headers=bearer(new_token))).status_code == 200
    assert (await client.post("/auth/onboarding/phone", headers=bearer(new_token),
                              json={"phone": "0244000009"})).status_code == 409
    refreshed = await client.post("/auth/refresh", json={"refresh_token": res.json()["refresh_token"]})
    assert refreshed.status_code == 200


# ── Phase 3: forgot password ──────────────────────────────────────────────


async def test_reset_via_otp_is_enumeration_safe_and_single_use(env):
    client, outbox = env
    email = f"rst-{uuid.uuid4().hex[:6]}@throwaway.test"
    temp = (await create_operator(client, admin_email=email)).json()["initial_admin"]["temp_password"]

    # Not onboarded yet: not resettable, but the response is identical.
    unknown = await client.post("/auth/reset/request", json={"email": f"nobody-{uuid.uuid4().hex}@throwaway.test"})
    pending = await client.post("/auth/reset/request", json={"email": email})
    assert unknown.status_code == pending.status_code == 200
    assert unknown.json() == pending.json()
    assert not any(kind == "reset" for kind, *_ in outbox.sent)

    await onboard(client, outbox, email, temp)
    session = (await login(client, email, NEW_PASSWORD)).json()

    res = await client.post("/auth/reset/request", json={"email": email.upper()})
    assert res.json() == unknown.json()
    kind, phone, code = outbox.last("reset")
    assert phone == "233244000009"

    wrong = "000000" if code != "000000" else "111111"
    bad_real = await client.post("/auth/reset/verify-otp", json={"email": email, "code": wrong})
    bad_fake = await client.post("/auth/reset/verify-otp", json={"email": "ghost@throwaway.test", "code": wrong})
    assert bad_real.status_code == bad_fake.status_code == 400
    assert bad_real.json() == bad_fake.json()

    res = await client.post("/auth/reset/verify-otp", json={"email": email, "code": code})
    assert res.status_code == 200
    grant = res.json()["reset_token"]
    assert (await client.post("/auth/reset/verify-otp", json={"email": email, "code": code})).status_code == 400

    # A reset grant is not a login token.
    assert (await client.get("/sessions", headers=bearer(grant))).status_code == 401

    res = await client.post("/auth/reset/set-password",
                            json={"reset_token": grant, "new_password": "weak", "confirm_password": "weak"})
    assert res.status_code == 400
    res = await client.post("/auth/reset/set-password",
                            json={"reset_token": grant, "new_password": NEWER_PASSWORD, "confirm_password": NEWER_PASSWORD})
    assert res.status_code == 200
    again = await client.post("/auth/reset/set-password",
                              json={"reset_token": grant, "new_password": "An0therOne", "confirm_password": "An0therOne"})
    assert again.status_code == 400  # single-use

    assert (await client.get("/sessions", headers=bearer(session["access_token"]))).status_code == 401
    assert (await client.post("/auth/refresh", json={"refresh_token": session["refresh_token"]})).status_code == 401
    assert (await login(client, email, NEW_PASSWORD)).status_code == 401
    assert (await login(client, email, NEWER_PASSWORD)).status_code == 200


async def test_reset_via_security_question_locks_after_three_misses(env):
    client, outbox = env
    email = f"sq-{uuid.uuid4().hex[:6]}@throwaway.test"
    temp = (await create_operator(client, admin_email=email)).json()["initial_admin"]["temp_password"]
    await onboard(client, outbox, email, temp, question="first_school", answer="Achimota")

    real = (await client.post("/auth/reset/security-question", json={"email": email})).json()
    assert real["question_key"] == "first_school"
    ghost_email = f"ghost-{uuid.uuid4().hex}@throwaway.test"
    ghost = (await client.post("/auth/reset/security-question", json={"email": ghost_email})).json()
    assert set(ghost) == set(real) and ghost == (await client.post(
        "/auth/reset/security-question", json={"email": ghost_email})).json()

    ghost_fail = await client.post("/auth/reset/security-question", json={"email": ghost_email, "answer": "x"})
    for _ in range(3):
        res = await client.post("/auth/reset/security-question", json={"email": email, "answer": "wrong"})
        assert res.status_code == 400 and res.json() == ghost_fail.json()
    locked = await client.post("/auth/reset/security-question", json={"email": email, "answer": " achimota "})
    assert locked.status_code == 400 and locked.json() == ghost_fail.json()
    assert (await admin_row(email)).security_answer_attempt_count == 3

    # An OTP reset clears the lock.
    await client.post("/auth/reset/request", json={"email": email})
    grant = (await client.post("/auth/reset/verify-otp",
                               json={"email": email, "code": outbox.last("reset")[2]})).json()["reset_token"]
    await client.post("/auth/reset/set-password",
                      json={"reset_token": grant, "new_password": NEWER_PASSWORD, "confirm_password": NEWER_PASSWORD})
    assert (await admin_row(email)).security_answer_attempt_count == 0

    res = await client.post("/auth/reset/security-question", json={"email": email, "answer": " ACHIMOTA"})
    assert res.status_code == 200
    res = await client.post("/auth/reset/set-password",
                            json={"reset_token": res.json()["reset_token"],
                                  "new_password": "Yet4notherPw", "confirm_password": "Yet4notherPw"})
    assert res.status_code == 200
    assert (await login(client, email, "Yet4notherPw")).status_code == 200


async def test_otp_codes_are_stored_hashed(env):
    client, outbox = env
    email = f"hash-{uuid.uuid4().hex[:6]}@throwaway.test"
    temp = (await create_operator(client, admin_email=email)).json()["initial_admin"]["temp_password"]
    token = (await login(client, email, temp)).json()["access_token"]
    await client.post("/auth/onboarding/phone", headers=bearer(token), json={"phone": "0244000009"})
    code = outbox.last("onboarding")[2]
    async with async_session_factory() as db:
        row = (await db.execute(select(AdminOtpCode).order_by(AdminOtpCode.created_at.desc()).limit(1))).scalar_one()
    assert code not in row.code_hash and re.match(r"^\$2[aby]\$", row.code_hash)


# ── Platform-owner password reset ─────────────────────────────────────────


async def platform_reset(client, operator_id, admin_id, body=None):
    return await client.post(
        f"/platform/operators/{operator_id}/admins/{admin_id}/reset-password",
        headers=client.owner_headers,
        json=body or {},
    )


async def test_platform_reset_with_verified_phone_forces_password_change_only(env):
    client, outbox = env
    email = f"pr-{uuid.uuid4().hex[:6]}@throwaway.test"
    created = (await create_operator(client, admin_email=email)).json()
    temp = created["initial_admin"]["temp_password"]
    await onboard(client, outbox, email, temp, question="birth_town", answer="Tema")
    session = (await login(client, email, NEW_PASSWORD)).json()
    admin_id = created["initial_admin"]["id"]

    # A phone may not be supplied for a verified admin.
    res = await platform_reset(client, created["id"], admin_id, {"phone": "0501112223"})
    assert res.status_code == 409

    res = await platform_reset(client, created["id"], admin_id)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mode"] == "temp_password" and body["sms_sent"] is True and body["temp_password"] is None
    assert body["sessions_revoked"] is True
    kind, phone, new_temp = outbox.last("temp_reset")
    assert phone == "233244000009"
    assert ("admin:" + admin_id, "platform:admin-password-reset", 5, 3600) in outbox.limiter_calls

    # Existing sessions and the old password are dead immediately.
    assert (await client.get("/sessions", headers=bearer(session["access_token"]))).status_code == 401
    assert (await client.post("/auth/refresh", json={"refresh_token": session["refresh_token"]})).status_code == 401
    assert (await login(client, email, NEW_PASSWORD)).status_code == 401

    # Audit trail.
    async with async_session_factory() as db:
        events = (await db.execute(select(AdminPasswordResetEvent).where(
            AdminPasswordResetEvent.admin_user_id == uuid.UUID(admin_id)))).scalars().all()
    assert len(events) == 1
    assert (events[0].platform_owner_id, events[0].mode, events[0].sms_sent) == (client.owner_id, "temp_password", True)
    assert str(events[0].id) == body["event_id"]

    res = await login(client, email, new_temp)
    assert res.status_code == 200
    data = res.json()
    assert data["must_change_password"] is True and data["must_complete_onboarding"] is False
    token = data["access_token"]
    gated = await client.get("/sessions", headers=bearer(token))
    assert gated.status_code == 403 and gated.headers.get("x-onboarding-required") == "1"
    status_ = (await client.get("/auth/onboarding/status", headers=bearer(token))).json()
    assert status_["mode"] == "change_password" and status_["has_security_question"]
    # No phone/OTP step for this mode.
    assert (await client.post("/auth/onboarding/phone", headers=bearer(token), json={"phone": "0244000009"})).status_code == 409

    res = await client.post("/auth/onboarding/password", headers=bearer(token),
                            json={"new_password": "weakpass", "confirm_password": "weakpass"})
    assert res.status_code == 400
    res = await client.post("/auth/onboarding/password", headers=bearer(token),
                            json={"new_password": NEWER_PASSWORD, "confirm_password": NEWER_PASSWORD})
    assert res.status_code == 200, res.text
    row = await admin_row(email)
    assert not row.must_change_password and not row.must_complete_onboarding
    assert row.security_question == "birth_town"  # untouched
    fresh = (await login(client, email, NEWER_PASSWORD)).json()
    assert (await client.get("/sessions", headers=bearer(fresh["access_token"]))).status_code == 200

    # The security question from onboarding still works for self-service reset.
    res = await client.post("/auth/reset/security-question", json={"email": email, "answer": "tema"})
    assert res.status_code == 200


async def test_platform_reset_without_verified_phone_restarts_onboarding(env):
    client, outbox = env
    email = f"pu-{uuid.uuid4().hex[:6]}@throwaway.test"
    created = (await create_operator(client, admin_email=email)).json()
    first_temp = created["initial_admin"]["temp_password"]
    admin_id = created["initial_admin"]["id"]

    # Pending onboarding code to the old number is voided by the reset.
    token = (await login(client, email, first_temp)).json()["access_token"]
    await client.post("/auth/onboarding/phone", headers=bearer(token), json={"phone": "0244000001"})
    stale_code = outbox.last("onboarding")[2]

    res = await platform_reset(client, created["id"], admin_id, {"phone": "050 111 2223"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mode"] == "onboarding" and body["phone_changed"] is True and body["temp_password"]
    assert body["phone"] == "+233 •••• ••• 223" and body["phone_verified"] is False
    assert outbox.last("temp_reset") == ("temp_reset", "233501112223", body["temp_password"])

    assert (await client.get("/auth/onboarding/status", headers=bearer(token))).status_code == 401
    assert (await login(client, email, first_temp)).status_code == 401
    row = await admin_row(email)
    assert row.must_complete_onboarding and not row.must_change_password and row.phone == "233501112223"

    new_token = (await login(client, email, body["temp_password"])).json()["access_token"]
    res = await client.post("/auth/onboarding/otp/verify", headers=bearer(new_token), json={"code": stale_code})
    assert res.status_code == 400
    status_ = (await client.get("/auth/onboarding/status", headers=bearer(new_token))).json()
    assert status_["mode"] == "onboarding" and status_["phone_on_file"] == "+233 •••• ••• 223"

    # Onboarding still requires the security question.
    await onboard_with_token(client, outbox, new_token, phone="0501112223")
    res = await client.post("/auth/onboarding/password", headers=bearer(new_token),
                            json={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    assert res.status_code == 400
    res = await client.post("/auth/onboarding/password", headers=bearer(new_token),
                            json={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD,
                                  "security_question": "first_employer", "security_answer": "MTN"})
    assert res.status_code == 200
    assert (await login(client, email, NEW_PASSWORD)).json()["must_complete_onboarding"] is False

    # Invalid phone -> 422, nothing changed.
    res = await platform_reset(client, created["id"], admin_id, {"phone": "12345"})
    assert res.status_code == 422


async def onboard_with_token(client, outbox, token, *, phone):
    res = await client.post("/auth/onboarding/phone", headers=bearer(token), json={"phone": phone})
    assert res.status_code == 200, res.text
    res = await client.post("/auth/onboarding/otp/verify", headers=bearer(token),
                            json={"code": outbox.last("onboarding")[2]})
    assert res.status_code == 200, res.text


async def test_platform_reset_is_scoped_to_the_operators_admins(env):
    client, _ = env
    op_a = (await create_operator(client, admin_email=f"sa-{uuid.uuid4().hex[:6]}@throwaway.test")).json()
    op_b = (await create_operator(client, admin_email=f"sb-{uuid.uuid4().hex[:6]}@throwaway.test")).json()
    # Admin of operator B addressed through operator A.
    res = await platform_reset(client, op_a["id"], op_b["initial_admin"]["id"])
    assert res.status_code == 404
    # A platform-owner id is never an operator admin.
    res = await platform_reset(client, op_a["id"], str(client.owner_id))
    assert res.status_code == 404
    res = await platform_reset(client, op_a["id"], str(uuid.uuid4()))
    assert res.status_code == 404
    # Operator admins can't call it.
    token = (await login(client, op_a["initial_admin"]["email"], op_a["initial_admin"]["temp_password"])).json()["access_token"]
    res = await client.post(f"/platform/operators/{op_a['id']}/admins/{op_a['initial_admin']['id']}/reset-password",
                            headers=bearer(token), json={})
    assert res.status_code == 401
    listed = (await client.get(f"/platform/operators/{op_a['id']}/admins", headers=client.owner_headers)).json()
    assert listed[0]["must_complete_onboarding"] is True and listed[0]["phone_verified"] is False
    assert listed[0]["phone"] == "+233 •••• ••• 001"


async def test_self_service_reset_clears_a_pending_platform_reset(env):
    client, outbox = env
    email = f"ps-{uuid.uuid4().hex[:6]}@throwaway.test"
    created = (await create_operator(client, admin_email=email)).json()
    await onboard(client, outbox, email, created["initial_admin"]["temp_password"])
    await platform_reset(client, created["id"], created["initial_admin"]["id"])
    assert (await admin_row(email)).must_change_password

    await client.post("/auth/reset/request", json={"email": email})
    grant = (await client.post("/auth/reset/verify-otp",
                               json={"email": email, "code": outbox.last("reset")[2]})).json()["reset_token"]
    res = await client.post("/auth/reset/set-password",
                            json={"reset_token": grant, "new_password": NEWER_PASSWORD, "confirm_password": NEWER_PASSWORD})
    assert res.status_code == 200
    res = await login(client, email, NEWER_PASSWORD)
    assert res.json()["must_change_password"] is False


# ── Self-service profile + password (platform owner and operator admin) ────


async def owner_password_change(client, current, new, confirm=None):
    return await client.post(
        "/platform/me/password",
        headers=client.owner_headers,
        json={"current_password": current, "new_password": new, "confirm_password": confirm or new},
    )


async def test_platform_me_edits_name_and_refuses_email(env):
    client, _ = env
    res = await client.patch("/platform/me", headers=client.owner_headers, json={"name": "  Renamed Owner "})
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "Renamed Owner"

    before = (await client.get("/platform/me", headers=client.owner_headers)).json()
    for payload, expect in [
        ({"email": "new@throwaway.test"}, 400),           # immutable identifier
        ({"name": "X", "email": "new@throwaway.test"}, 400),
        ({"password_hash": "x"}, 400),
        ({"is_active": False}, 400),
        ({}, 400),                                         # nothing to update
        ({"name": ""}, 400),                               # empty name
    ]:
        res = await client.patch("/platform/me", headers=client.owner_headers, json=payload)
        assert res.status_code == expect, (payload, res.status_code, res.text)
    assert (await client.get("/platform/me", headers=client.owner_headers)).json() == before


async def test_platform_owner_password_change_keeps_this_session_and_kills_others(env):
    client, _ = env
    # A second signed-in session for the same owner.
    me = (await client.get("/platform/me", headers=client.owner_headers)).json()
    other = await client.post("/platform/auth/login", json={"email": me["email"], "password": OWNER_PASSWORD})
    other_token = other.json()["access_token"]
    other_refresh = other.json()["refresh_token"]
    assert (await client.get("/platform/me", headers=bearer(other_token))).status_code == 200

    for payload, expect in [
        (("wrong-password", NEW_PASSWORD, NEW_PASSWORD), "current password is incorrect"),
        ((OWNER_PASSWORD, "weakpass", "weakpass"), "at least 8"),
        ((OWNER_PASSWORD, NEW_PASSWORD, NEW_PASSWORD + "x"), "don't match"),
        ((OWNER_PASSWORD, OWNER_PASSWORD, OWNER_PASSWORD), "different from your current"),
    ]:
        res = await owner_password_change(client, *payload)
        assert res.status_code == 400 and expect in res.json()["detail"], res.text

    res = await owner_password_change(client, OWNER_PASSWORD, NEW_PASSWORD)
    assert res.status_code == 200, res.text
    fresh = res.json()
    assert fresh["access_token"] and fresh["refresh_token"]

    # The other session and its refresh token are dead; the fresh pair works.
    assert (await client.get("/platform/me", headers=bearer(other_token))).status_code == 401
    assert (await client.post("/platform/auth/refresh", json={"refresh_token": other_refresh})).status_code == 401
    assert (await client.get("/platform/me", headers=bearer(fresh["access_token"]))).status_code == 200
    assert (await client.post("/platform/auth/refresh", json={"refresh_token": fresh["refresh_token"]})).status_code == 200

    # Old password no longer signs in; new one does.
    assert (await client.post("/platform/auth/login", json={"email": me["email"], "password": OWNER_PASSWORD})).status_code == 401
    relog = await client.post("/platform/auth/login", json={"email": me["email"], "password": NEW_PASSWORD})
    assert relog.status_code == 200
    client.owner_headers = {"Authorization": f"Bearer {relog.json()['access_token']}"}
    # Restore for other tests in this module.
    assert (await owner_password_change(client, NEW_PASSWORD, OWNER_PASSWORD)).status_code == 200
    client.owner_headers = {"Authorization": f"Bearer {(await client.post('/platform/auth/login', json={'email': me['email'], 'password': OWNER_PASSWORD})).json()['access_token']}"}


async def test_operator_profile_edit_validates_and_refuses_protected_fields(env):
    client, _ = env
    created = (await create_operator(client, admin_email=f"op-{uuid.uuid4().hex[:6]}@throwaway.test")).json()
    url = f"/platform/operators/{created['id']}"
    # Unique per run: this module's tests share one database.
    billing_email = f"billing-{uuid.uuid4().hex[:8]}@isp.test"

    res = await client.patch(url, headers=client.owner_headers,
                             json={"name": " Renamed ISP ", "contact_email": f"  {billing_email.upper()} ", "contact_phone": "024 400 5566"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["name"] == "Renamed ISP"
    assert body["contact_email"] == billing_email           # normalized (lowercased, trimmed)
    assert body["contact_phone"] == "233244005566"          # normalized

    for payload, expect in [
        ({"slug": "new-slug"}, 400),
        ({"monthly_fee_ghs": "1.00"}, 400),
        ({"billing_status": "active"}, 400),
        ({"status": "suspended"}, 400),
        ({"trial_ends_at": "2027-01-01T00:00:00Z"}, 400),
        ({"name": "OK", "slug": "sneaky"}, 400),           # mixed payload refused whole
        ({"contact_email": "not-an-email"}, 400),
        ({"contact_phone": "12345"}, 400),
        ({}, 400),
    ]:
        res = await client.patch(url, headers=client.owner_headers, json=payload)
        assert res.status_code == expect, (payload, res.status_code, res.text)

    after = (await client.get(url, headers=client.owner_headers)).json()
    assert (after["slug"], after["name"], after["contact_email"]) == (created["slug"], "Renamed ISP", billing_email)

    # Clearing the phone is allowed; a duplicate contact email is refused.
    assert (await client.patch(url, headers=client.owner_headers, json={"contact_phone": ""})).status_code == 200
    assert (await client.get(url, headers=client.owner_headers)).json()["contact_phone"] is None
    other = (await create_operator(client, admin_email=f"op2-{uuid.uuid4().hex[:6]}@throwaway.test")).json()
    res = await client.patch(f"/platform/operators/{other['id']}", headers=client.owner_headers,
                             json={"contact_email": billing_email})
    assert res.status_code == 409

    assert (await client.patch(f"/platform/operators/{uuid.uuid4()}", headers=client.owner_headers,
                               json={"name": "Ghost"})).status_code == 404
    # Operator admins cannot reach it.
    admin_token = (await login(client, created["initial_admin"]["email"],
                               created["initial_admin"]["temp_password"])).json()["access_token"]
    assert (await client.patch(url, headers=bearer(admin_token), json={"name": "X"})).status_code == 401


async def test_operator_admin_password_change_keeps_this_session_and_kills_others(env):
    client, outbox = env
    email = f"chg-{uuid.uuid4().hex[:6]}@throwaway.test"
    created = (await create_operator(client, admin_email=email)).json()
    await onboard(client, outbox, email, created["initial_admin"]["temp_password"])

    first = (await login(client, email, NEW_PASSWORD)).json()
    second = (await login(client, email, NEW_PASSWORD)).json()
    assert (await client.get("/sessions", headers=bearer(second["access_token"]))).status_code == 200

    async def change(current, new, confirm=None):
        return await client.post("/auth/me/password", headers=bearer(first["access_token"]),
                                 json={"current_password": current, "new_password": new,
                                       "confirm_password": confirm or new})

    for payload, expect in [
        (("wrong-password", NEWER_PASSWORD, NEWER_PASSWORD), "current password is incorrect"),
        ((NEW_PASSWORD, "weakpass", "weakpass"), "at least 8"),
        ((NEW_PASSWORD, NEWER_PASSWORD, NEWER_PASSWORD + "x"), "don't match"),
        ((NEW_PASSWORD, NEW_PASSWORD, NEW_PASSWORD), "different from your current"),
    ]:
        res = await change(*payload)
        assert res.status_code == 400 and expect in res.json()["detail"], res.text

    res = await change(NEW_PASSWORD, NEWER_PASSWORD)
    assert res.status_code == 200, res.text
    fresh = res.json()
    assert fresh["must_complete_onboarding"] is False and fresh["must_change_password"] is False

    # Caller keeps working with the fresh pair; the other session is gone.
    assert (await client.get("/sessions", headers=bearer(fresh["access_token"]))).status_code == 200
    assert (await client.get("/sessions", headers=bearer(first["access_token"]))).status_code == 401
    assert (await client.get("/sessions", headers=bearer(second["access_token"]))).status_code == 401
    assert (await client.post("/auth/refresh", json={"refresh_token": second["refresh_token"]})).status_code == 401
    assert (await login(client, email, NEW_PASSWORD)).status_code == 401
    assert (await login(client, email, NEWER_PASSWORD)).status_code == 200


async def test_password_change_is_refused_while_onboarding_is_pending(env):
    client, _ = env
    email = f"pend-{uuid.uuid4().hex[:6]}@throwaway.test"
    created = (await create_operator(client, admin_email=email)).json()
    token = (await login(client, email, created["initial_admin"]["temp_password"])).json()["access_token"]
    res = await client.post("/auth/me/password", headers=bearer(token),
                            json={"current_password": created["initial_admin"]["temp_password"],
                                  "new_password": NEWER_PASSWORD, "confirm_password": NEWER_PASSWORD})
    assert res.status_code == 403 and res.headers.get("x-onboarding-required") == "1"


@pytest.mark.parametrize("phone,stored", [
    ("0244123456", "233244123456"),
    ("024 412 3456", "233244123456"),
    ("+233 24 412 3456", "233244123456"),
    ("233244123456", "233244123456"),
    ("", None),        # explicit clear
    ("   ", None),     # whitespace only clears too
])
async def test_operator_contact_phone_normalization(env, phone, stored):
    client, _ = env
    op = (await create_operator(client, admin_email=f"ph-{uuid.uuid4().hex[:6]}@throwaway.test")).json()
    res = await client.patch(f"/platform/operators/{op['id']}", headers=client.owner_headers,
                             json={"contact_phone": phone})
    assert res.status_code == 200, res.text
    assert res.json()["contact_phone"] == stored


@pytest.mark.parametrize("phone", ["12345", "0144123456", "+44 7700 900123", "not a phone", "0244 12345"])
async def test_operator_contact_phone_rejects_bad_numbers(env, phone):
    client, _ = env
    op = (await create_operator(client, admin_email=f"phx-{uuid.uuid4().hex[:6]}@throwaway.test")).json()
    before = (await client.get(f"/platform/operators/{op['id']}", headers=client.owner_headers)).json()
    res = await client.patch(f"/platform/operators/{op['id']}", headers=client.owner_headers,
                             json={"contact_phone": phone})
    assert res.status_code == 400, res.text
    after = (await client.get(f"/platform/operators/{op['id']}", headers=client.owner_headers)).json()
    assert after["contact_phone"] == before["contact_phone"]


@pytest.mark.parametrize("email", [
    "not-an-email", "missing@domain", "@no-local.test", "two @spaces.test", "", "   ", "a@b@c.test",
])
async def test_operator_contact_email_rejects_invalid_addresses(env, email):
    client, _ = env
    op = (await create_operator(client, admin_email=f"em-{uuid.uuid4().hex[:6]}@throwaway.test")).json()
    before = (await client.get(f"/platform/operators/{op['id']}", headers=client.owner_headers)).json()
    res = await client.patch(f"/platform/operators/{op['id']}", headers=client.owner_headers,
                             json={"contact_email": email})
    assert res.status_code == 400, (email, res.text)
    after = (await client.get(f"/platform/operators/{op['id']}", headers=client.owner_headers)).json()
    assert after["contact_email"] == before["contact_email"]  # unchanged on rejection


async def test_operator_edit_is_field_by_field(env):
    client, _ = env
    op = (await create_operator(client, admin_email=f"one-{uuid.uuid4().hex[:6]}@throwaway.test")).json()
    url = f"/platform/operators/{op['id']}"
    original = (await client.get(url, headers=client.owner_headers)).json()

    # Touching only the name leaves contact details alone, and vice versa.
    res = await client.patch(url, headers=client.owner_headers, json={"name": "Only Name"})
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "Only Name"
    assert (body["contact_email"], body["contact_phone"]) == (original["contact_email"], original["contact_phone"])
    assert body["slug"] == original["slug"] and body["billing_status"] == original["billing_status"]

    new_email = f"only-{uuid.uuid4().hex[:8]}@isp.test"
    res = await client.patch(url, headers=client.owner_headers, json={"contact_email": new_email})
    assert res.status_code == 200 and res.json()["name"] == "Only Name"
    assert res.json()["contact_email"] == new_email


async def test_platform_me_name_is_trimmed_and_bounded(env):
    client, _ = env
    res = await client.patch("/platform/me", headers=client.owner_headers, json={"name": "  Spaced  Out  "})
    assert res.status_code == 200 and res.json()["name"] == "Spaced  Out"
    assert (await client.patch("/platform/me", headers=client.owner_headers,
                               json={"name": "x" * 256})).status_code == 400
    assert (await client.patch("/platform/me", headers=client.owner_headers,
                               json={"name": "   "})).status_code == 400
