"""scripts/platform_owner_recover.py — the break-glass tool — against a REAL
database, run exactly as an operator would run it: as a subprocess, with the
confirmation typed on stdin.

For each broken state (locked password, locked character code, security setup
abandoned at every step, setup complete with the code lost) it checks the row
afterwards AND that the owner can actually get back in through the app. Then
the safety rails: no change without the exact phrase, on EOF, on --dry-run, on
an unknown or inactive account, or if the row changes while the prompt waits.

Only runs against a disposable database:

    PLATFORM_OWNER_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    from src.db.models import PlatformOwner, PlatformOwnerOtpCode, PlatformOwnerSecurityEvent
    from src.modules.admin_accounts import otp as otp_service
    from src.modules.auth.tokens import platform_owner_token_response
    from src.modules.platform import character_code
    from src.modules.platform import routes as platform_routes
    from src.utils.auth import hash_password

BACKEND = Path(__file__).resolve().parents[1]
PASSWORD = "Owner-Passw0rd"
PHONE = "233244000777"


# ── Harness ───────────────────────────────────────────────────────────────


async def recover(*args: str, stdin: str | None = None) -> tuple[int, str]:
    """Run the script as a subprocess; returns (exit code, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "scripts.platform_owner_recover", *args,
        cwd=str(BACKEND), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(None if stdin is None else stdin.encode()), timeout=60)
    return proc.returncode, out.decode()


async def confirm(action: str, email: str, *extra: str) -> tuple[int, str]:
    return await recover(action, "--email", email, "--no-sms", *extra, stdin=f"{action} {email}\n")


@pytest_asyncio.fixture(loop_scope="module")
async def client(monkeypatch_module):
    async def no_limit(key, bucket, limit=10, window_seconds=60):
        return None

    monkeypatch_module.setattr(platform_routes, "enforce_rate_limit", no_limit)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as c:
        yield c


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


async def make_owner(**values) -> tuple["PlatformOwner", str | None]:
    """values may include with_code=True (stores a code; returns it)."""
    with_code = values.pop("with_code", False)
    confirmed = values.pop("confirmed", False)
    async with async_session_factory() as db:
        owner = PlatformOwner(
            email=f"Recover-{uuid.uuid4().hex[:10]}@Throwaway.Test", password_hash=hash_password(PASSWORD),
            name="Owner", is_active=values.pop("is_active", True), **values,
        )
        db.add(owner)
        await db.flush()
        code = None
        if with_code:
            code = character_code.generate_code()
            storage = character_code.build_storage(owner.id, code)
            if confirmed:
                storage["confirmed_at"] = datetime.now(timezone.utc).isoformat()
            owner.challenge_hashes = storage
        await db.commit()
        return owner, code


async def row(owner_id) -> "PlatformOwner":
    async with async_session_factory() as db:
        return await db.get(PlatformOwner, owner_id)


async def events(owner_id):
    async with async_session_factory() as db:
        return (await db.execute(select(PlatformOwnerSecurityEvent).where(
            PlatformOwnerSecurityEvent.platform_owner_id == owner_id))).scalars().all()


SNAPSHOT_FIELDS = [
    "must_complete_security_setup", "phone", "phone_verified", "security_question", "security_answer_hash",
    "security_answer_attempt_count", "challenge_hashes", "challenge_set_at", "challenge_pending_positions",
    "challenge_pending_jti", "challenge_attempt_count", "challenge_locked_until", "login_attempt_count",
    "login_locked_until", "token_version", "password_hash",
]


async def snapshot(owner_id) -> dict:
    o = await row(owner_id)
    return {f: getattr(o, f) for f in SNAPSHOT_FIELDS}


def future(hours=2):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


async def login(client, owner):
    return await client.post("/platform/auth/login", json={"email": owner.email, "password": PASSWORD})


async def answer(client, code, body):
    chars = {str(p): code[p - 1] for p in body["positions"]}
    return await client.post("/platform/auth/challenge", json={"challenge_token": body["challenge_token"], "characters": chars})


# ── Broken state 1: password lockout ──────────────────────────────────────


async def test_clear_login_lock_restores_sign_in(client):
    owner, _ = await make_owner(login_attempt_count=5, login_locked_until=future(1))
    assert (await login(client, owner)).status_code == 401, "precondition: locked out"

    rc, out = await confirm("clear-login-lock", owner.email)
    assert rc == 0, out
    fresh = await row(owner.id)
    assert (fresh.login_attempt_count, fresh.login_locked_until) == (0, None)
    assert fresh.token_version == 0, "clearing a lock must not sign anyone out"
    res = await login(client, owner)
    assert res.status_code == 200 and "access_token" in res.json()
    (evt,) = await events(owner.id)
    assert evt.event_type == "break_glass" and evt.detail["action"] == "clear-login-lock"
    assert set(evt.detail["fields"]) == {"login_attempt_count", "login_locked_until"}


async def test_clear_login_lock_leaves_the_challenge_lock_alone(client):
    owner, _ = await make_owner(login_attempt_count=5, login_locked_until=future(1),
                                challenge_attempt_count=5, challenge_locked_until=future(3))
    rc, out = await confirm("clear-login-lock", owner.email)
    assert rc == 0, out
    fresh = await row(owner.id)
    assert fresh.challenge_attempt_count == 5 and fresh.challenge_locked_until is not None


# ── Broken state 2: character-code lockout ────────────────────────────────


async def test_clear_challenge_lock_restores_sign_in_with_the_same_positions(client):
    owner, code = await make_owner(must_complete_security_setup=False, with_code=True, confirmed=True,
                                   challenge_attempt_count=5, challenge_locked_until=future(3),
                                   challenge_pending_positions=[1, 5, 9], challenge_pending_jti=uuid.uuid4())
    assert (await login(client, owner)).status_code == 403, "precondition: code entry locked"
    old_token = (await row(owner.id)).challenge_pending_jti
    # Set AFTER the precondition sign-in: a correct password clears this
    # counter by design, before the challenge lock answers.
    async with async_session_factory() as db:
        o = await db.get(PlatformOwner, owner.id)
        o.login_attempt_count = 2
        await db.commit()

    rc, out = await confirm("clear-challenge-lock", owner.email)
    assert rc == 0, out
    fresh = await row(owner.id)
    assert (fresh.challenge_attempt_count, fresh.challenge_locked_until, fresh.challenge_pending_jti) == (0, None, None)
    assert fresh.challenge_pending_positions == [1, 5, 9], "no fresh draw of positions"
    assert fresh.login_attempt_count == 2, "the password counter is a separate lock"
    assert fresh.challenge_hashes is not None and fresh.must_complete_security_setup is False
    assert old_token is not None

    body = (await login(client, owner)).json()
    assert body["challenge_required"] is True and body["positions"] == [2, 6, 10]
    res = await answer(client, code, body)
    assert res.status_code == 200 and "access_token" in res.json()


# ── Broken state 3: setup abandoned at every step, or code lost after ─────


SETUP_STATES = {
    "nothing_done": {},
    "phone_only": {"phone": PHONE, "phone_verified": True},
    "phone_and_question": {"phone": PHONE, "phone_verified": True, "security_question": "birth_town",
                           "security_answer_hash": "$2b$12$placeholderplaceholderplaceholderplaceholde"},
    "code_generated_unconfirmed": {"phone": PHONE, "phone_verified": True, "security_question": "birth_town",
                                   "security_answer_hash": "x", "with_code": True,
                                   "challenge_pending_positions": [0, 3, 7]},
    "all_steps_done_not_completed": {"phone": PHONE, "phone_verified": True, "security_question": "birth_town",
                                     "security_answer_hash": "x", "with_code": True, "confirmed": True},
    "completed_but_code_lost_and_locked": {"phone": PHONE, "phone_verified": True, "security_question": "first_school",
                                           "security_answer_hash": "x", "with_code": True, "confirmed": True,
                                           "must_complete_security_setup": False, "security_answer_attempt_count": 3,
                                           "challenge_attempt_count": 5, "challenge_locked_until": future(3),
                                           "login_attempt_count": 5, "login_locked_until": future(1),
                                           "challenge_pending_positions": [2, 4, 6],
                                           "challenge_pending_jti": uuid.uuid4()},
}


@pytest.mark.parametrize("state", list(SETUP_STATES))
async def test_reset_setup_returns_every_state_to_a_clean_start(client, state):
    values = dict(SETUP_STATES[state])
    values.setdefault("must_complete_security_setup", True)
    owner, _ = await make_owner(**values, token_version=3)
    old_session = platform_owner_token_response(await row(owner.id)).access_token
    password_before = (await row(owner.id)).password_hash
    async with async_session_factory() as db:  # an open setup code that must die
        await otp_service.issue_code(db, platform_owner_id=owner.id, purpose="setup", phone=PHONE)
        await db.commit()

    rc, out = await confirm("reset-setup", owner.email)
    assert rc == 0, out

    fresh = await row(owner.id)
    assert fresh.must_complete_security_setup is True
    assert (fresh.phone, fresh.phone_verified) == (None, False)
    assert (fresh.security_question, fresh.security_answer_hash, fresh.security_answer_attempt_count) == (None, None, 0)
    assert (fresh.challenge_hashes, fresh.challenge_set_at, fresh.challenge_pending_positions, fresh.challenge_pending_jti) == (None,) * 4
    assert (fresh.challenge_attempt_count, fresh.challenge_locked_until) == (0, None)
    assert (fresh.login_attempt_count, fresh.login_locked_until) == (0, None)
    assert fresh.token_version == 4, "every session signed out"
    assert fresh.password_hash == password_before, "the password is never changed"
    async with async_session_factory() as db:
        open_codes = (await db.execute(select(func.count()).select_from(PlatformOwnerOtpCode).where(
            PlatformOwnerOtpCode.platform_owner_id == owner.id, PlatformOwnerOtpCode.consumed_at.is_(None)))).scalar()
    assert open_codes == 0

    # The old session is dead; the password alone gets back in, as a
    # setup-pending owner, and setup shows every step to do.
    assert (await client.get("/platform/setup/status", headers={"Authorization": f"Bearer {old_session}"})).status_code == 401
    res = await login(client, owner)
    assert res.status_code == 200 and "access_token" in res.json(), res.text
    status_body = (await client.get("/platform/setup/status",
                                    headers={"Authorization": f"Bearer {res.json()['access_token']}"})).json()
    assert status_body["must_complete_security_setup"] is True
    assert not (status_body["phone_verified"] or status_body["has_security_question"] or status_body["has_character_code"])

    (evt,) = [e for e in await events(owner.id) if e.event_type == "break_glass"]
    assert evt.detail["action"] == "reset-setup" and evt.detail["otp_codes_voided"] == 1


# ── Safety rails ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("typed", [
    "",                                   # just pressed enter
    "yes",
    "reset-setup",                        # action without the account
    "clear-login-lock {email}",           # a different action
    "reset-setup someone-else@throwaway.test",
    "reset-setup {email_lower}x",
])
async def test_anything_but_the_exact_phrase_changes_nothing(typed):
    owner, _ = await make_owner(with_code=True, must_complete_security_setup=False, phone=PHONE, phone_verified=True,
                                login_attempt_count=5, login_locked_until=future(1))
    before = await snapshot(owner.id)
    rc, out = await recover("reset-setup", "--email", owner.email, "--no-sms",
                            stdin=typed.format(email=owner.email, email_lower=owner.email.lower()) + "\n")
    assert rc == 1, out
    assert "Nothing was changed" in out
    assert await snapshot(owner.id) == before
    assert await events(owner.id) == []


async def test_no_input_at_all_changes_nothing():
    owner, _ = await make_owner(login_attempt_count=5, login_locked_until=future(1))
    before = await snapshot(owner.id)
    rc, out = await recover("clear-login-lock", "--email", owner.email, "--no-sms", stdin="")
    assert rc == 1 and "No confirmation received" in out
    assert await snapshot(owner.id) == before


async def test_the_phrase_matches_the_email_case_insensitively_found_but_typed_as_stored():
    """Found by email in any case, but the phrase is the email as stored and
    printed — the operator copies what the tool shows them."""
    owner, _ = await make_owner(login_attempt_count=5, login_locked_until=future(1))
    rc, out = await recover("clear-login-lock", "--email", owner.email.lower(), "--no-sms",
                            stdin=f"clear-login-lock {owner.email}\n")
    assert rc == 0, out
    assert f"type exactly:  clear-login-lock {owner.email}" in out


async def test_dry_run_prints_the_plan_and_changes_nothing():
    owner, _ = await make_owner(with_code=True, phone=PHONE, phone_verified=True, login_attempt_count=5,
                                login_locked_until=future(1), must_complete_security_setup=False)
    before = await snapshot(owner.id)
    rc, out = await recover("reset-setup", "--email", owner.email, "--dry-run", stdin=f"reset-setup {owner.email}\n")
    assert rc == 0 and "--dry-run: nothing was changed" in out
    for field in ("must_complete_security_setup", "phone", "challenge_hashes", "login_locked_until", "token_version"):
        assert field in out, field
    assert "type exactly" not in out, "a dry run never asks"
    assert await snapshot(owner.id) == before and await events(owner.id) == []


async def test_nothing_to_do_is_reported_and_writes_nothing():
    owner, _ = await make_owner()
    rc, out = await recover("clear-challenge-lock", "--email", owner.email, stdin="")
    assert rc == 0 and "Nothing to change" in out
    assert await events(owner.id) == []


async def test_a_change_while_the_prompt_waits_aborts():
    owner, _ = await make_owner(login_attempt_count=5, login_locked_until=future(1))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "scripts.platform_owner_recover", "clear-login-lock", "--email", owner.email, "--no-sms",
        cwd=str(BACKEND), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    seen = b""
    while b"type exactly" not in seen:
        chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=60)
        assert chunk, seen.decode()
        seen += chunk
    # The lock lapses and someone else signs in, changing the counters.
    async with async_session_factory() as db:
        o = await db.get(PlatformOwner, owner.id)
        o.login_attempt_count = 1
        await db.commit()
    proc.stdin.write(f"clear-login-lock {owner.email}\n".encode())
    await proc.stdin.drain()
    proc.stdin.close()
    rest, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    out = (seen + rest).decode()
    assert proc.returncode == 1 and "changed while waiting" in out, out
    fresh = await row(owner.id)
    assert fresh.login_attempt_count == 1 and fresh.login_locked_until is not None
    assert await events(owner.id) == []


async def test_unknown_email_and_missing_email_are_usage_errors():
    rc, out = await recover("reset-setup", "--email", f"nobody-{uuid.uuid4().hex[:6]}@throwaway.test", stdin="x\n")
    assert rc == 2 and "No platform owner" in out
    rc, out = await recover("reset-setup", stdin="x\n")
    assert rc == 2 and "needs --email" in out


async def test_deactivated_owner_is_refused():
    owner, _ = await make_owner(is_active=False, login_attempt_count=5, login_locked_until=future(1))
    before = await snapshot(owner.id)
    rc, out = await recover("clear-login-lock", "--email", owner.email, stdin=f"clear-login-lock {owner.email}\n")
    assert rc == 2 and "deactivated" in out
    assert await snapshot(owner.id) == before


async def test_output_never_shows_secrets():
    owner, code = await make_owner(with_code=True, confirmed=True, phone=PHONE, phone_verified=True,
                                   security_question="birth_town", security_answer_hash=hash_password("accra"),
                                   must_complete_security_setup=False)
    stored = await row(owner.id)
    rc, out = await confirm("reset-setup", owner.email)
    assert rc == 0, out
    assert PHONE not in out, "phone must be masked"
    assert code not in out
    assert stored.security_answer_hash not in out and stored.password_hash not in out
    for digest in stored.challenge_hashes["digests"]:
        assert digest not in out
    rc, status_out = await recover("status", "--email", owner.email)
    assert rc == 0 and PHONE not in status_out


async def test_status_is_read_only():
    owner, _ = await make_owner(login_attempt_count=5, login_locked_until=future(1))
    before = await snapshot(owner.id)
    rc, out = await recover("status", "--email", owner.email)
    assert rc == 0 and "password failures / lock" in out and "ACTIVE" in out
    assert await snapshot(owner.id) == before and await events(owner.id) == []


async def test_alert_outcome_is_recorded_when_sms_is_not_skipped():
    """Without --no-sms, an owner with a verified phone is texted (best
    effort). This network has no route out, so the send fails — which is
    exactly the case to prove: the recovery still completes, and the failure
    is recorded on the event and reported."""
    owner, _ = await make_owner(phone=PHONE, phone_verified=True, login_attempt_count=5, login_locked_until=future(1))
    rc, out = await recover("clear-login-lock", "--email", owner.email, stdin=f"clear-login-lock {owner.email}\n")
    assert rc == 0, out
    assert (await row(owner.id)).login_locked_until is None
    (evt,) = await events(owner.id)
    assert evt.sms_sent is True or evt.sms_error, "the outcome must be recorded either way"
    assert "Alert text" in out
