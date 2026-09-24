"""The seed's non-production opt-in that creates the platform owner with
security setup already complete (SEED_OWNER_SETUP_COMPLETE).

Two layers:
  * the opt-in rules themselves — pure functions, no database;
  * the real seed, run as a subprocess against a throwaway database, proving
    the owner it creates can sign in through the challenge, and that it is
    insert-only: an existing owner is never touched, whatever the variables say.

The second layer needs a disposable database:

    PLATFORM_OWNER_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

from src.db.seeds.seed import SeedRefused, owner_setup_opt_in
from src.modules.platform.owner_setup import is_valid_code, looks_like_test_email

FLOW_DB = os.getenv("PLATFORM_OWNER_FLOW_TEST_DATABASE_URL", "")
DB_OK = bool(FLOW_DB) and "throwaway" in FLOW_DB.rsplit("/", 1)[-1] and os.getenv("DATABASE_URL") == FLOW_DB
needs_db = pytest.mark.skipif(not DB_OK, reason="needs PLATFORM_OWNER_FLOW_TEST_DATABASE_URL (a throwaway database) == DATABASE_URL")

CODE = "ABCDEFGHJK23"
FLAG, CODE_VAR = "SEED_OWNER_SETUP_COMPLETE", "SEED_OWNER_CHARACTER_CODE"
BACKEND = Path(__file__).resolve().parents[1]


# ── The rules (no database) ───────────────────────────────────────────────


@pytest.mark.parametrize("env", [{}, {FLAG: ""}, {FLAG: "false"}, {FLAG: "0"}, {FLAG: "no"}, {FLAG: " False "},
                                 {CODE_VAR: CODE}, {FLAG: "false", CODE_VAR: CODE}])
def test_off_unless_explicitly_enabled(env):
    assert owner_setup_opt_in(env, "owner@ci.test") is None


@pytest.mark.parametrize("value", ["ture", "on", "enabled", "2", "yes please"])
def test_a_typo_in_the_flag_is_refused_not_guessed(value):
    with pytest.raises(SeedRefused, match="is not true/false"):
        owner_setup_opt_in({FLAG: value, CODE_VAR: CODE}, "owner@ci.test")


@pytest.mark.parametrize("code", ["", "ABC", "ABCDEFGHJK234", "ABCDEFGHJK2O", "ABCDEFGHJK21", "ABCDEFGHJKIL", "ABCDEF GHJK2"])
def test_invalid_codes_are_refused(code):
    with pytest.raises(SeedRefused, match="missing or invalid"):
        owner_setup_opt_in({FLAG: "true", CODE_VAR: code}, "owner@ci.test")


@pytest.mark.parametrize("email", ["owner@yourdomain.com", "owner@gmail.com", "owner@test.com", "a@examples.com",
                                   "a@example.com.evil.org", "a@localhost", "a@test", "not-an-email", ""])
def test_real_looking_owner_emails_are_refused(email):
    with pytest.raises(SeedRefused, match="not a reserved test address"):
        owner_setup_opt_in({FLAG: "true", CODE_VAR: CODE}, email)


@pytest.mark.parametrize("email", ["owner@ci.test", "a@example.com", "a@example.org", "a@example.net", "a@x.example",
                                   "a@foo.invalid", "a@dev.localhost", "a@box.local", "Owner@CI.TEST"])
def test_enabled_with_a_test_address_returns_the_code(email):
    assert owner_setup_opt_in({FLAG: "true", CODE_VAR: CODE}, email) == CODE


def test_code_is_normalised_to_uppercase():
    assert owner_setup_opt_in({FLAG: "YES", CODE_VAR: " abcdefghjk23 "}, "owner@ci.test") == CODE


def test_helpers():
    assert is_valid_code(CODE) and not is_valid_code(CODE.lower()) and not is_valid_code(None)
    assert looks_like_test_email("x@y.test") and not looks_like_test_email("x@y.com")


# ── The real seed (throwaway database) ────────────────────────────────────


def run_seed(email: str, **env) -> tuple[int, str]:
    async def go():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "src.db.seeds.seed", cwd=str(BACKEND),
            env={**os.environ, "PLATFORM_OWNER_EMAIL": email, "PLATFORM_OWNER_PASSWORD": "Seed-Passw0rd", **env},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
        return proc.returncode, out.decode()
    return asyncio.run(go())


async def _owner(email):
    from sqlalchemy import select

    from src.db.base import async_session_factory, engine
    from src.db.models import PlatformOwner, PlatformOwnerSecurityEvent
    try:
        async with async_session_factory() as db:
            owner = (await db.execute(select(PlatformOwner).where(PlatformOwner.email == email))).scalar_one_or_none()
            events = [] if owner is None else (await db.execute(select(PlatformOwnerSecurityEvent.event_type).where(
                PlatformOwnerSecurityEvent.platform_owner_id == owner.id))).scalars().all()
            return owner, events
    finally:
        await engine.dispose()


def owner_row(email):
    return asyncio.run(_owner(email))


def unique_email():
    return f"seed-{uuid.uuid4().hex[:10]}@ci.test"


@needs_db
def test_opt_in_creates_a_set_up_owner_who_can_sign_in_with_the_code():
    email = unique_email()
    rc, out = run_seed(email, **{FLAG: "true", CODE_VAR: CODE})
    assert rc == 0, out
    assert "Security setup marked complete" in out
    assert CODE not in out, "the seed must not print the code"
    owner, events = owner_row(email)
    assert owner.must_complete_security_setup is False
    assert owner.phone_verified is True and owner.phone
    assert owner.security_question and owner.security_answer_hash
    assert owner.challenge_hashes and owner.challenge_hashes.get("confirmed_at")
    assert CODE not in str(owner.challenge_hashes)
    assert events == ["seeded_setup_complete"]

    async def sign_in():
        import httpx

        from src.app import app
        from src.db.base import engine
        from src.modules.platform import routes as platform_routes

        async def no_limit(*a, **k):
            return None
        platform_routes.enforce_rate_limit = no_limit
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t/api/v1") as c:
                res = await c.post("/platform/auth/login", json={"email": email, "password": "Seed-Passw0rd"})
                body = res.json()
                assert body.get("challenge_required") is True, body
                chars = {str(p): CODE[p - 1] for p in body["positions"]}
                res = await c.post("/platform/auth/challenge", json={"challenge_token": body["challenge_token"], "characters": chars})
                assert res.status_code == 200, res.text
                me = await c.get("/platform/me", headers={"Authorization": f"Bearer {res.json()['access_token']}"})
                assert me.status_code == 200, "the setup gate must be open for a seeded-complete owner"
        finally:
            await engine.dispose()
    asyncio.run(sign_in())


@needs_db
def test_rerunning_with_a_different_code_never_touches_the_owner():
    email = unique_email()
    assert run_seed(email, **{FLAG: "true", CODE_VAR: CODE})[0] == 0
    before, _ = owner_row(email)
    rc, out = run_seed(email, **{FLAG: "true", CODE_VAR: "ZZZZZZZZZZZZ"})
    assert rc == 0, out
    assert "ignored: the seed never modifies an existing owner" in out
    after, events = owner_row(email)
    assert after.challenge_hashes == before.challenge_hashes and after.token_version == before.token_version
    assert events == ["seeded_setup_complete"]


@needs_db
def test_an_existing_setup_pending_owner_is_left_pending():
    """The production-shaped case: the owner already exists (setup pending)
    and someone runs the seed with the opt-in on. Nothing changes."""
    email = unique_email()
    assert run_seed(email)[0] == 0  # opt-in off: creates the owner, setup pending
    before, _ = owner_row(email)
    assert before.must_complete_security_setup is True and before.challenge_hashes is None
    rc, out = run_seed(email, **{FLAG: "true", CODE_VAR: CODE})
    assert rc == 0 and "ignored" in out, out
    after, events = owner_row(email)
    assert after.must_complete_security_setup is True and after.challenge_hashes is None and after.phone is None
    assert events == []


@needs_db
def test_default_seed_is_unchanged_owner_starts_setup_pending():
    email = unique_email()
    rc, out = run_seed(email)
    assert rc == 0, out
    owner, events = owner_row(email)
    assert owner.must_complete_security_setup is True and owner.challenge_hashes is None
    assert events == []


@needs_db
@pytest.mark.parametrize("env,why", [
    ({FLAG: "true", CODE_VAR: CODE}, "production-like email"),
    ({FLAG: "true", CODE_VAR: "SHORT"}, "invalid code"),
    ({FLAG: "maybe", CODE_VAR: CODE}, "typo in the flag"),
])
def test_a_refused_opt_in_writes_nothing_at_all(env, why):
    email = f"seed-{uuid.uuid4().hex[:10]}@realcompany.com" if why == "production-like email" else unique_email()
    rc, out = run_seed(email, **env)
    assert rc == 2, out
    assert "SEED REFUSED, nothing was written" in out
    assert "Seeding database" not in out, "refusal must happen before any database work"
    owner, _ = owner_row(email)
    assert owner is None
