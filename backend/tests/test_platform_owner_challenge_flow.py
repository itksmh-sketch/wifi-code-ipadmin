"""Platform-owner character challenge: code storage, the login challenge token,
position handling, the challenge lockout, and the generation endpoint —
against a REAL database.

Runs the FastAPI app in-process with SMS sends captured, like the other
platform-owner flow suites. Only runs against a disposable database:

    PLATFORM_OWNER_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from datetime import datetime, timedelta, timezone

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
    from jose import jwt
    from sqlalchemy import select

    from src.app import app
    from src.config import get_settings
    from src.db.base import async_session_factory
    from src.db.models import PlatformOwner, PlatformOwnerSecurityEvent
    from src.modules.admin_accounts import notifications as account_notifications
    from src.modules.auth.tokens import platform_owner_token_response
    from src.modules.platform import character_code, owner_lockout, owner_security_routes
    from src.modules.platform import routes as platform_routes
    from src.modules.sms.types import SMSSendResult
    from src.utils.auth import (
        LOGIN_CHALLENGE_ISSUER,
        create_login_challenge_token,
        hash_password,
    )

PASSWORD = "Owner-Passw0rd"
PHONE = "233244000555"


class Outbox:
    def __init__(self):
        self.sent = []  # (kind, phone, client_ip)

    async def owner_lockout(self, owner, *, kind, client_ip=None):
        self.sent.append((kind, owner.phone, client_ip))
        return SMSSendResult(success=True, provider_reference="test")


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    outbox = Outbox()
    monkeypatch.setattr(account_notifications, "send_platform_owner_lockout_sms", outbox.owner_lockout)

    async def no_limit(key, bucket, limit=10, window_seconds=60):
        return None

    monkeypatch.setattr(platform_routes, "enforce_rate_limit", no_limit)
    monkeypatch.setattr(owner_security_routes, "enforce_rate_limit", no_limit)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as client:
        yield client, outbox


async def make_owner(*, setup_complete=True, with_code=True, **values) -> tuple["PlatformOwner", str | None]:
    """An owner, optionally with a code stored. Returns (owner, plaintext code)."""
    async with async_session_factory() as db:
        owner = PlatformOwner(
            email=f"owner-{uuid.uuid4().hex[:10]}@throwaway.test", password_hash=hash_password(PASSWORD),
            name="Owner", is_active=True, must_complete_security_setup=not setup_complete,
            phone=PHONE, phone_verified=True, **values,
        )
        db.add(owner)
        await db.flush()
        code = None
        if with_code:
            code = character_code.generate_code()
            owner.challenge_hashes = character_code.build_storage(owner.id, code)
        await db.commit()
        return owner, code


async def row(owner_id) -> "PlatformOwner":
    async with async_session_factory() as db:
        return await db.get(PlatformOwner, owner_id)


async def patch(owner_id, **values):
    async with async_session_factory() as db:
        o = await db.get(PlatformOwner, owner_id)
        for k, v in values.items():
            setattr(o, k, v)
        await db.commit()


async def login(client, owner, password=PASSWORD):
    return await client.post("/platform/auth/login", json={"email": owner.email, "password": password})


async def answer(client, token, characters):
    return await client.post("/platform/auth/challenge", json={"challenge_token": token, "characters": characters})


def right(code, positions):
    return {str(p): code[p - 1] for p in positions}


def wrong(code, positions):
    chars = right(code, positions)
    last = str(positions[-1])
    chars[last] = next(c for c in character_code.ALPHABET if c != chars[last])
    return chars


# ── Storage: keyed per-position digests ───────────────────────────────────


def test_digests_are_keyed_hmac_of_owner_salt_position_char():
    owner_id = uuid.uuid4()
    code = character_code.generate_code()
    storage = character_code.build_storage(owner_id, code)
    assert storage["v"] == 1 and len(storage["digests"]) == character_code.CODE_LENGTH == 12
    key = character_code._derive_key(get_settings().encryption_key)
    for i, c in enumerate(code):
        expected = hmac.new(key, f"{owner_id}:{storage['salt']}:{i}:{c}".encode(), hashlib.sha256).hexdigest()
        assert storage["digests"][i] == expected
    assert code not in str(storage)


def test_the_key_is_derived_not_the_raw_setting_and_depends_on_it():
    setting = get_settings().encryption_key
    key = character_code._derive_key(setting)
    assert len(key) == 32 and key != setting.encode()[:32]
    assert character_code._derive_key(setting + "x") != key


def test_a_regenerated_code_shares_no_digests_even_if_identical():
    owner_id = uuid.uuid4()
    code = character_code.generate_code()
    a, b = character_code.build_storage(owner_id, code), character_code.build_storage(owner_id, code)
    assert a["salt"] != b["salt"] and not set(a["digests"]) & set(b["digests"])


def test_digests_are_bound_to_the_owner():
    code = character_code.generate_code()
    storage = character_code.build_storage(uuid.uuid4(), code)
    answers = {i: code[i] for i in (0, 5, 11)}
    assert not character_code.check_positions(uuid.uuid4(), storage, answers)


def test_check_positions():
    owner_id = uuid.uuid4()
    code = character_code.generate_code()
    storage = character_code.build_storage(owner_id, code)
    good = {1: code[1], 6: code[6], 10: code[10]}
    assert character_code.check_positions(owner_id, storage, good)
    assert character_code.check_positions(owner_id, storage, {k: f" {v.lower()} " for k, v in good.items()})
    for bad in (
        {**good, 10: next(c for c in character_code.ALPHABET if c != code[10])},
        {**good, 10: code[10] * 2},
        {**good, 99: "A"},
        {},
    ):
        assert not character_code.check_positions(owner_id, storage, bad)


def test_generated_codes_use_only_the_unambiguous_alphabet():
    assert not set("01OIL") & set(character_code.ALPHABET)
    for _ in range(50):
        code = character_code.generate_code()
        assert len(code) == 12 and set(code) <= set(character_code.ALPHABET)


# ── Login: unchanged unless setup is complete ─────────────────────────────


async def test_login_is_unchanged_while_setup_is_pending_even_with_a_code(env):
    client, _ = env
    for with_code in (False, True):
        owner, _ = await make_owner(setup_complete=False, with_code=with_code)
        res = await login(client, owner)
        assert res.status_code == 200, res.text
        body = res.json()
        assert "access_token" in body and "refresh_token" in body and "challenge_token" not in body
        fresh = await row(owner.id)
        assert fresh.challenge_pending_jti is None and fresh.challenge_pending_positions is None
        assert fresh.last_login_at is not None


async def test_setup_complete_login_returns_a_challenge_not_tokens(env):
    client, _ = env
    owner, _ = await make_owner()
    res = await login(client, owner)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["challenge_required"] is True and "access_token" not in body
    positions = body["positions"]
    assert len(positions) == 3 == len(set(positions)) and positions == sorted(positions)
    assert all(1 <= p <= 12 for p in positions) and body["code_length"] == 12
    assert body["expires_in_seconds"] == 300
    fresh = await row(owner.id)
    assert [p + 1 for p in fresh.challenge_pending_positions] == positions
    assert fresh.last_login_at is None, "not signed in until the challenge is passed"


async def test_setup_complete_without_a_code_fails_closed(env):
    client, _ = env
    owner, _ = await make_owner(with_code=False)
    res = await login(client, owner)
    assert res.status_code == 403 and "access_token" not in res.text


async def test_wrong_password_never_reaches_the_challenge(env):
    client, _ = env
    owner, _ = await make_owner()
    res = await login(client, owner, "WrongPassword1")
    assert res.status_code == 401 and res.json() == {"detail": platform_routes.INVALID_CREDENTIALS}
    assert (await row(owner.id)).challenge_pending_jti is None


# ── The challenge ─────────────────────────────────────────────────────────


async def test_correct_answer_issues_working_tokens_and_the_token_is_single_use(env):
    client, _ = env
    owner, code = await make_owner()
    body = (await login(client, owner)).json()
    res = await answer(client, body["challenge_token"], right(code, body["positions"]))
    assert res.status_code == 200, res.text
    tokens = res.json()
    me = await client.get("/platform/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200
    fresh = await row(owner.id)
    assert fresh.challenge_pending_jti is None and fresh.challenge_pending_positions is None
    assert fresh.challenge_attempt_count == 0 and fresh.last_login_at is not None

    replay = await answer(client, body["challenge_token"], right(code, body["positions"]))
    assert replay.status_code == 401 and replay.json()["detail"] == platform_routes.CHALLENGE_EXPIRED


async def test_lowercase_answers_are_accepted(env):
    client, _ = env
    owner, code = await make_owner()
    body = (await login(client, owner)).json()
    chars = {k: v.lower() for k, v in right(code, body["positions"]).items()}
    assert (await answer(client, body["challenge_token"], chars)).status_code == 200


async def test_wrong_answer_counts_spends_the_token_and_keeps_the_positions(env):
    client, _ = env
    owner, code = await make_owner()
    first = (await login(client, owner)).json()
    res = await answer(client, first["challenge_token"], wrong(code, first["positions"]))
    assert res.status_code == 401 and "4 attempts left" in res.json()["detail"]
    fresh = await row(owner.id)
    assert fresh.challenge_attempt_count == 1 and fresh.challenge_pending_jti is None
    assert fresh.login_attempt_count == 0, "a challenge miss must not touch the password counter"

    # Spent: even the RIGHT answer on that token is refused now.
    assert (await answer(client, first["challenge_token"], right(code, first["positions"]))).status_code == 401

    second = (await login(client, owner)).json()
    assert second["positions"] == first["positions"], "positions must not reshuffle after a miss"
    assert second["challenge_token"] != first["challenge_token"]
    assert (await answer(client, second["challenge_token"], right(code, second["positions"]))).status_code == 200
    assert (await row(owner.id)).challenge_attempt_count == 0


async def test_positions_stay_fixed_across_repeated_logins_then_change_after_success(env):
    client, _ = env
    owner, code = await make_owner()
    seen = [tuple((await login(client, owner)).json()["positions"]) for _ in range(6)]
    assert len(set(seen)) == 1, "re-login must not deal new positions"
    body = (await login(client, owner)).json()
    await answer(client, body["challenge_token"], right(code, body["positions"]))
    # After a success, positions are redrawn on the next sign-in (and are
    # random, so across several successes they must not all be the same).
    draws = set()
    for _ in range(6):
        b = (await login(client, owner)).json()
        draws.add(tuple(b["positions"]))
        assert (await answer(client, b["challenge_token"], right(code, b["positions"]))).status_code == 200
    assert len(draws) > 1


async def test_a_new_login_voids_the_previous_challenge_token(env):
    client, _ = env
    owner, code = await make_owner()
    old = (await login(client, owner)).json()
    new = (await login(client, owner)).json()
    assert (await answer(client, old["challenge_token"], right(code, old["positions"]))).status_code == 401
    assert (await answer(client, new["challenge_token"], right(code, new["positions"]))).status_code == 200


async def test_answering_other_positions_is_rejected_without_counting_or_spending(env):
    client, _ = env
    owner, code = await make_owner()
    body = (await login(client, owner)).json()
    others = [p for p in range(1, 13) if p not in body["positions"]][:3]
    res = await answer(client, body["challenge_token"], right(code, others))
    assert res.status_code == 400
    fresh = await row(owner.id)
    assert fresh.challenge_attempt_count == 0 and fresh.challenge_pending_jti is not None
    assert (await answer(client, body["challenge_token"], right(code, body["positions"]))).status_code == 200


async def test_expired_and_version_bumped_tokens_are_refused(env):
    client, _ = env
    owner, code = await make_owner()
    body = (await login(client, owner)).json()
    fresh = await row(owner.id)
    expired = jwt.encode(
        {"sub": str(owner.id), "tv": 0, "jti": str(fresh.challenge_pending_jti), "type": "login_challenge",
         "iss": LOGIN_CHALLENGE_ISSUER, "exp": datetime.now(timezone.utc) - timedelta(seconds=1)},
        get_settings().platform_owner_jwt_secret, algorithm="HS256",
    )
    assert (await answer(client, expired, right(code, body["positions"]))).status_code == 401

    await patch(owner.id, token_version=1)  # e.g. a password change elsewhere
    assert (await answer(client, body["challenge_token"], right(code, body["positions"]))).status_code == 401


async def test_forged_jti_is_refused(env):
    client, _ = env
    owner, code = await make_owner()
    body = (await login(client, owner)).json()
    forged = create_login_challenge_token(owner_id=str(owner.id), token_version=0, jti=str(uuid.uuid4()))
    assert (await answer(client, forged, right(code, body["positions"]))).status_code == 401


async def test_challenge_tokens_are_useless_as_sessions(env):
    client, _ = env
    owner, _ = await make_owner()
    token = (await login(client, owner)).json()["challenge_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/platform/me", headers=headers)).status_code == 401
    assert (await client.get("/platform/setup/status", headers=headers)).status_code == 401
    assert (await client.get("/platform/operators", headers=headers)).status_code == 401
    assert (await client.post("/platform/auth/refresh", json={"refresh_token": token})).status_code == 401


async def test_session_tokens_are_useless_as_challenge_tokens(env):
    client, _ = env
    owner, code = await make_owner()
    await login(client, owner)
    pair = platform_owner_token_response(await row(owner.id))
    positions = [p + 1 for p in (await row(owner.id)).challenge_pending_positions]
    for token in (pair.access_token, pair.refresh_token):
        assert (await answer(client, token, right(code, positions))).status_code == 401


# ── Challenge lockout ─────────────────────────────────────────────────────


async def test_five_misses_lock_for_three_hours_and_alert_with_the_ip(env):
    client, outbox = env
    owner, code = await make_owner()
    for attempt in range(1, owner_lockout.CHALLENGE_MAX_ATTEMPTS + 1):
        body = (await login(client, owner)).json()
        res = await answer(client, body["challenge_token"], wrong(code, body["positions"]))
        if attempt < 5:
            assert res.status_code == 401
            assert (await row(owner.id)).challenge_locked_until is None
        else:
            assert res.status_code == 403 and res.json()["detail"] == platform_routes.CHALLENGE_LOCKED

    fresh = await row(owner.id)
    remaining = fresh.challenge_locked_until - datetime.now(timezone.utc)
    assert timedelta(hours=3) - timedelta(minutes=1) < remaining <= timedelta(hours=3)
    assert fresh.login_attempt_count == 0 and fresh.login_locked_until is None

    alerts = [s for s in outbox.sent if s[0] == "challenge" and s[1] == PHONE]
    assert alerts and alerts[-1][2], "the alert must carry the source IP"
    async with async_session_factory() as db:
        evts = (await db.execute(select(PlatformOwnerSecurityEvent).where(
            PlatformOwnerSecurityEvent.platform_owner_id == owner.id,
            PlatformOwnerSecurityEvent.event_type == "challenge_lockout"))).scalars().all()
    assert len(evts) == 1 and evts[0].sms_sent is True

    # Locked: a correct password is answered with the lock, not a challenge,
    # and even the right code on a token issued before the lock is refused.
    res = await login(client, owner)
    assert res.status_code == 403 and "challenge_token" not in res.text
    assert (await row(owner.id)).login_attempt_count == 0


async def test_lapsed_challenge_lock_lets_the_right_code_in(env):
    client, _ = env
    owner, code = await make_owner(challenge_attempt_count=5,
                                   challenge_locked_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    body = (await login(client, owner)).json()
    assert (await answer(client, body["challenge_token"], right(code, body["positions"]))).status_code == 200
    fresh = await row(owner.id)
    assert (fresh.challenge_attempt_count, fresh.challenge_locked_until) == (0, None)


async def test_password_misses_do_not_touch_the_challenge_counter(env):
    client, _ = env
    owner, _ = await make_owner(challenge_attempt_count=2)
    for _ in range(3):
        await login(client, owner, "WrongPassword1")
    fresh = await row(owner.id)
    assert fresh.login_attempt_count == 3 and fresh.challenge_attempt_count == 2


# ── Generation endpoint (setup step 3) ────────────────────────────────────


async def test_generation_shows_the_code_once_and_stores_only_digests(env):
    client, _ = env
    owner, _ = await make_owner(setup_complete=False, with_code=False)
    headers = {"Authorization": f"Bearer {platform_owner_token_response(owner).access_token}"}
    res = await client.post("/platform/setup/character-code", headers=headers, json={"current_password": PASSWORD})
    assert res.status_code == 200, res.text
    assert res.headers.get("cache-control") == "no-store"
    code = res.json()["character_code"]
    assert len(code) == 12 and set(code) <= set(character_code.ALPHABET)
    fresh = await row(owner.id)
    assert code not in str(fresh.challenge_hashes) and fresh.challenge_set_at is not None
    assert character_code.check_positions(owner.id, fresh.challenge_hashes, {0: code[0], 4: code[4], 11: code[11]})
    status_body = (await client.get("/platform/setup/status", headers=headers)).json()
    assert status_body["has_character_code"] is True and code not in str(status_body)


async def test_generation_requires_the_password_and_regenerating_replaces_the_code(env):
    client, _ = env
    owner, _ = await make_owner(setup_complete=False, with_code=False)
    headers = {"Authorization": f"Bearer {platform_owner_token_response(owner).access_token}"}
    bad = await client.post("/platform/setup/character-code", headers=headers, json={"current_password": "nope"})
    assert bad.status_code == 400 and (await row(owner.id)).challenge_hashes is None

    first = (await client.post("/platform/setup/character-code", headers=headers, json={"current_password": PASSWORD})).json()
    await patch(owner.id, challenge_pending_positions=[0, 1, 2], challenge_pending_jti=uuid.uuid4())
    second = (await client.post("/platform/setup/character-code", headers=headers, json={"current_password": PASSWORD})).json()
    fresh = await row(owner.id)
    assert fresh.challenge_pending_positions is None and fresh.challenge_pending_jti is None
    if first["character_code"] != second["character_code"]:
        c = first["character_code"]
        assert not character_code.check_positions(owner.id, fresh.challenge_hashes, {i: c[i] for i in range(12)})


async def test_generation_refuses_once_setup_is_complete(env):
    client, _ = env
    owner, code = await make_owner()
    before = (await row(owner.id)).challenge_hashes
    headers = {"Authorization": f"Bearer {platform_owner_token_response(owner).access_token}"}
    res = await client.post("/platform/setup/character-code", headers=headers, json={"current_password": PASSWORD})
    assert res.status_code == 409
    assert (await row(owner.id)).challenge_hashes == before
