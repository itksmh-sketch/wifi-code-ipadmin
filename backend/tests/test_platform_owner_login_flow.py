"""Platform-owner password login: lockout, timing equalisation, email case, and
the removed duplicate endpoints — against a REAL database.

Runs the FastAPI app in-process (no server) with SMS sends captured instead of
sent, following tests/test_admin_security_flow.py. It writes platform owners
and security events, so it only runs when explicitly pointed at a disposable
database:

    PLATFORM_OWNER_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>

and skips otherwise — including in the production container, whose
DATABASE_URL is the live database.

A lapsed lock is exercised by writing the stored deadline into the past, not
by waiting (see the note at the top of test_admin_security_flow.py).
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time
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
    from sqlalchemy import select

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import PlatformOwner, PlatformOwnerSecurityEvent
    from src.modules.admin_accounts import notifications as account_notifications
    from src.modules.auth.tokens import platform_owner_token_response
    from src.modules.platform import character_code, owner_lockout
    from src.modules.platform import routes as platform_routes
    from src.modules.platform.owner_lockout import LOGIN_LOCKOUT_HOURS, LOGIN_MAX_ATTEMPTS
    from src.modules.sms.types import SMSSendResult
    from src.utils.auth import hash_password

PASSWORD = "Owner-Passw0rd"
NEW_PASSWORD = "Ev3nStrongerPass"
WRONG = "WrongPassword1"
PHONE = "233244000222"


class Outbox:
    def __init__(self):
        self.sent = []  # (kind, phone)

    async def owner_lockout(self, owner, *, kind, client_ip=None):
        self.sent.append((f"owner_lockout_{kind}", owner.phone))
        return SMSSendResult(success=True, provider_reference="test")

    def count(self, kind):
        return sum(1 for item in self.sent if item[0] == kind)


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    outbox = Outbox()
    # owner_lockout.send_lockout_notification imports this at call time, so
    # patching the module attribute catches the background task too.
    monkeypatch.setattr(account_notifications, "send_platform_owner_lockout_sms", outbox.owner_lockout)

    async def no_limit(key, bucket, limit=10, window_seconds=60):
        return None

    monkeypatch.setattr(platform_routes, "enforce_rate_limit", no_limit)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as client:
        yield client, outbox


async def make_owner(*, email: str | None = None, phone: str | None = None, active: bool = True) -> str:
    email = email or f"owner-{uuid.uuid4().hex[:10]}@throwaway.test"
    async with async_session_factory() as db:
        db.add(
            PlatformOwner(
                email=email,
                password_hash=hash_password(PASSWORD),
                name="Owner",
                is_active=active,
                phone=phone,
                phone_verified=phone is not None,
            )
        )
        await db.commit()
    return email


async def row(email) -> "PlatformOwner":
    async with async_session_factory() as db:
        return (await db.execute(select(PlatformOwner).where(PlatformOwner.email == email))).scalar_one()


async def patch_owner(email, **values):
    async with async_session_factory() as db:
        owner = (await db.execute(select(PlatformOwner).where(PlatformOwner.email == email))).scalar_one()
        for key, value in values.items():
            setattr(owner, key, value)
        await db.commit()


async def events(email, event_type=None):
    owner = await row(email)
    async with async_session_factory() as db:
        stmt = select(PlatformOwnerSecurityEvent).where(PlatformOwnerSecurityEvent.platform_owner_id == owner.id)
        if event_type:
            stmt = stmt.where(PlatformOwnerSecurityEvent.event_type == event_type)
        return (await db.execute(stmt.order_by(PlatformOwnerSecurityEvent.created_at))).scalars().all()


async def login(client, email, password=PASSWORD):
    return await client.post("/platform/auth/login", json={"email": email, "password": password})


def assert_generic_401(res):
    assert res.status_code == 401, res.text
    assert res.json() == {"detail": platform_routes.INVALID_CREDENTIALS}


# ── Lockout ───────────────────────────────────────────────────────────────


async def test_login_locks_at_exactly_five_failures(env):
    client, outbox = env
    email = await make_owner(phone=PHONE)

    for attempt in range(1, LOGIN_MAX_ATTEMPTS):
        assert_generic_401(await login(client, email, WRONG))
        owner = await row(email)
        assert owner.login_attempt_count == attempt
        assert owner.login_locked_until is None, f"locked early at attempt {attempt}"

    # The correct password still works right up to the threshold, and resets.
    assert (await login(client, email)).status_code == 200
    assert (await row(email)).login_attempt_count == 0

    for _ in range(LOGIN_MAX_ATTEMPTS):
        assert_generic_401(await login(client, email, WRONG))

    owner = await row(email)
    remaining = owner.login_locked_until - datetime.now(timezone.utc)
    hours = timedelta(hours=LOGIN_LOCKOUT_HOURS)
    assert LOGIN_LOCKOUT_HOURS == 1
    assert hours - timedelta(minutes=1) < remaining <= hours

    # Locked even with the RIGHT password, indistinguishable from a wrong one.
    assert_generic_401(await login(client, email))

    assert outbox.count("owner_lockout_login") == 1
    evts = await events(email, "login_lockout")
    assert len(evts) == 1
    assert evts[0].sms_sent is True
    assert evts[0].detail["attempts"] == LOGIN_MAX_ATTEMPTS


async def test_lockout_sms_is_not_repeated_while_locked(env):
    client, outbox = env
    email = await make_owner(phone=PHONE)
    for _ in range(LOGIN_MAX_ATTEMPTS):
        await login(client, email, WRONG)
    before = outbox.count("owner_lockout_login")

    for _ in range(5):
        assert_generic_401(await login(client, email, WRONG))
        assert_generic_401(await login(client, email))

    assert outbox.count("owner_lockout_login") == before, "a live lock must not re-notify"
    assert len(await events(email, "login_lockout")) == 1
    assert (await row(email)).login_attempt_count == LOGIN_MAX_ATTEMPTS


async def test_lockout_without_verified_phone_records_why_no_sms(env):
    client, outbox = env
    email = await make_owner()  # no phone: today's state for the real owner
    before = outbox.count("owner_lockout_login")
    for _ in range(LOGIN_MAX_ATTEMPTS):
        await login(client, email, WRONG)

    assert outbox.count("owner_lockout_login") == before
    (evt,) = await events(email, "login_lockout")
    assert evt.sms_sent is False
    assert evt.sms_error == "no_verified_phone"


async def test_lapsed_lock_starts_a_fresh_run(env):
    client, _ = env
    email = await make_owner()
    for _ in range(LOGIN_MAX_ATTEMPTS):
        await login(client, email, WRONG)
    await patch_owner(email, login_locked_until=datetime.now(timezone.utc) - timedelta(seconds=1))

    assert_generic_401(await login(client, email, WRONG))
    owner = await row(email)
    assert owner.login_attempt_count == 1
    assert owner.login_locked_until is None

    # ...and a lapsed lock lets the right password straight back in.
    await patch_owner(email, login_attempt_count=LOGIN_MAX_ATTEMPTS,
                      login_locked_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert (await login(client, email)).status_code == 200
    owner = await row(email)
    assert (owner.login_attempt_count, owner.login_locked_until) == (0, None)


async def test_concurrent_wrong_guesses_each_count(env):
    client, _ = env
    email = await make_owner()
    results = await asyncio.gather(*(login(client, email, WRONG) for _ in range(3)))
    assert all(r.status_code == 401 for r in results)
    assert (await row(email)).login_attempt_count == 3


async def test_password_change_clears_the_login_lock(env):
    client, _ = env
    email = await make_owner()
    # /platform/me/password is gated on security setup, so this owner has
    # completed it (with a code on file, which login then challenges for).
    owner = await row(email)
    await patch_owner(email, must_complete_security_setup=False,
                      challenge_hashes=character_code.build_storage(owner.id, character_code.generate_code()))
    # A session opened before the lock (e.g. on another device).
    token = platform_owner_token_response(await row(email)).access_token
    for _ in range(LOGIN_MAX_ATTEMPTS):
        await login(client, email, WRONG)
    assert owner_lockout.is_locked(await row(email), owner_lockout.LOGIN)

    res = await client.post(
        "/platform/me/password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
    )
    assert res.status_code == 200, res.text
    owner = await row(email)
    assert (owner.login_attempt_count, owner.login_locked_until) == (0, None)
    res = await login(client, email, NEW_PASSWORD)
    assert res.status_code == 200 and res.json()["challenge_required"] is True


# ── Enumeration safety and timing ─────────────────────────────────────────


async def test_every_failure_mode_answers_identically(env):
    client, _ = env
    locked = await make_owner()
    for _ in range(LOGIN_MAX_ATTEMPTS):
        await login(client, locked, WRONG)
    inactive = await make_owner(active=False)
    wrong = await make_owner()

    for res in (
        await login(client, f"nobody-{uuid.uuid4().hex[:6]}@throwaway.test", WRONG),
        await login(client, inactive),  # right password, inactive account
        await login(client, wrong, WRONG),
        await login(client, locked),  # right password, locked
    ):
        assert_generic_401(res)


async def test_every_path_does_exactly_one_bcrypt_check(env, monkeypatch):
    """The structural half of timing equalisation: unknown-email and locked
    paths must spend the same work as a real password check."""
    client, _ = env
    real = platform_routes.verify_password
    calls = []

    def spy(plain, hashed):
        calls.append(hashed)
        return real(plain, hashed)

    monkeypatch.setattr(platform_routes, "verify_password", spy)

    locked = await make_owner()
    for _ in range(LOGIN_MAX_ATTEMPTS):
        await login(client, locked, WRONG)
    ok = await make_owner()

    for label, email, pw in (
        ("unknown", f"nobody-{uuid.uuid4().hex[:6]}@throwaway.test", WRONG),
        ("locked", locked, PASSWORD),
        ("wrong", ok, WRONG),
        ("correct", ok, PASSWORD),
    ):
        calls.clear()
        await login(client, email, pw)
        assert len(calls) == 1, f"{label}: {len(calls)} bcrypt checks"
        assert calls[0].startswith("$2b$12$"), f"{label}: dummy hash cost differs from stored hashes"


async def test_unknown_email_takes_about_as_long_as_wrong_password(env):
    """Wall-clock half. bcrypt dominates each request by two orders of
    magnitude, so a coarse ratio is stable; without the dummy check the
    unknown-email path would be ~100x faster, not merely 'a bit'."""
    client, _ = env
    email = await make_owner()

    async def median_ms(e, pw, n=5):
        samples = []
        for _ in range(n):
            t = time.perf_counter()
            await login(client, e, pw)
            samples.append((time.perf_counter() - t) * 1000)
        return statistics.median(samples)

    await login(client, email, PASSWORD)  # warm-up; also keeps the counter at 0
    unknown = await median_ms(f"nobody-{uuid.uuid4().hex[:6]}@throwaway.test", WRONG, n=4)
    wrong = await median_ms(email, WRONG, n=4)
    assert 0.5 < unknown / wrong < 2.0, f"unknown={unknown:.0f}ms wrong={wrong:.0f}ms"


# ── Email case ────────────────────────────────────────────────────────────


async def test_email_match_is_case_and_whitespace_insensitive(env):
    client, _ = env
    email = await make_owner()
    for variant in (email.upper(), f"  {email}  ", email.title()):
        res = await login(client, variant)
        assert res.status_code == 200, f"{variant!r}: {res.text}"


async def test_stored_mixed_case_email_matches_lowercase_input(env):
    client, _ = env
    tag = uuid.uuid4().hex[:8]
    email = await make_owner(email=f"Mixed-{tag}@Throwaway.Test")
    assert (await login(client, email.lower())).status_code == 200


async def test_case_variants_share_one_lockout_counter(env):
    """Rotating the case of the address must not buy fresh attempts."""
    client, _ = env
    email = await make_owner()
    for variant in (email, email.upper(), email.title(), email.swapcase(), f" {email} "):
        await login(client, variant, WRONG)
    assert owner_lockout.is_locked(await row(email), owner_lockout.LOGIN)


# ── Removed duplicates ────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/auth/platform/login", "/auth/platform/refresh"])
async def test_removed_duplicate_endpoints_are_gone(env, path):
    client, _ = env
    email = await make_owner()
    payload = {"email": email, "password": PASSWORD, "refresh_token": "x"}
    res = await client.post(path, json=payload)
    assert res.status_code == 404, f"{path} -> {res.status_code}"
