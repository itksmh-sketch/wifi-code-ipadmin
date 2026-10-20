"""Reseller sign-in against a REAL database: the email matches whatever its
case, and an unknown email, a wrong password and a disabled account (by the
operator, or through the operator's logins being off) answer identically, each
after exactly one bcrypt check — as admin login does.

Only runs against a disposable database:

    RESELLER_LOGIN_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>

(tests/throwaway_db.py run sets both.)
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("RESELLER_LOGIN_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs RESELLER_LOGIN_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import ISPOperator, Reseller
    from src.modules.resellers import routes as reseller_routes
    from src.utils.reseller_auth import hash_reseller_password

PASSWORD = "Str0ngPassword"


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    async def no_limit(*args, **kwargs):
        return None

    monkeypatch.setattr(reseller_routes, "enforce_rate_limit", no_limit)

    # Records which hash every reseller login verified against.
    checked: list[str] = []
    real_verify = reseller_routes.verify_reseller_password

    def recording_verify(plain, hashed):
        checked.append(hashed)
        return real_verify(plain, hashed)

    monkeypatch.setattr(reseller_routes, "verify_reseller_password", recording_verify)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as client:
        yield {"client": client, "checked": checked}


async def make_operator(**values) -> "ISPOperator":
    slug = f"rsl-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Rsl {slug}", slug=slug, contact_email=f"{slug}@throwaway.test",
                               status="approved", **values)
        db.add(operator)
        await db.commit()
        return operator


async def make_reseller(operator, **values) -> "Reseller":
    async with async_session_factory() as db:
        # Lowercase, as admin_routes stores every address.
        state = dict(isp_operator_id=operator.id, name="Res", email=f"res-{uuid.uuid4().hex[:10]}@throwaway.test",
                     password_hash=hash_reseller_password(PASSWORD), role="reseller", is_active=True)
        reseller = Reseller(**{**state, **values})
        db.add(reseller)
        await db.commit()
        return reseller


async def sign_in(env, email, password=PASSWORD):
    env["checked"].clear()
    res = await env["client"].post("/reseller/auth/login", json={"email": email, "password": password})
    return res, list(env["checked"])


def same_reply(a, b):
    assert a.status_code == b.status_code == 401
    assert a.content == b.content == b'{"detail":"Invalid email or password"}'
    assert {k: v for k, v in a.headers.items() if k != "date"} == {k: v for k, v in b.headers.items() if k != "date"}


async def test_email_matches_whatever_its_case(env):
    reseller = await make_reseller(await make_operator())
    for typed in (reseller.email, reseller.email.upper(), f"  {reseller.email.title()} "):
        res, checked = await sign_in(env, typed)
        assert res.status_code == 200, (typed, res.text)
        assert checked == [reseller.password_hash]


async def test_wrong_password_in_another_case_is_still_refused(env):
    """Negative: matching the email case-insensitively must not loosen the
    password check."""
    reseller = await make_reseller(await make_operator())
    res, checked = await sign_in(env, reseller.email.upper(), PASSWORD.lower())
    assert res.status_code == 401
    assert checked == [reseller.password_hash]


async def test_case_insensitive_match_does_not_reach_a_lookalike(env):
    """Negative: case folding only; a different address never matches."""
    reseller = await make_reseller(await make_operator())
    res, checked = await sign_in(env, reseller.email.replace("@", "x@").upper())
    assert res.status_code == 401
    assert checked == [reseller_routes._DUMMY_HASH]


async def test_unknown_wrong_password_and_disabled_answer_identically(env):
    operator = await make_operator()
    active = await make_reseller(operator)
    disabled = await make_reseller(operator, is_active=False)
    blocked_operator = await make_operator(admin_logins_disabled_at=datetime.now(timezone.utc))
    blocked = await make_reseller(blocked_operator)

    unknown, via_unknown = await sign_in(env, f"ghost-{uuid.uuid4().hex}@throwaway.test")
    wrong, via_wrong = await sign_in(env, active.email, "N0tThePassword")
    off, via_off = await sign_in(env, disabled.email)
    off_by_operator, via_operator = await sign_in(env, blocked.email.upper())

    for reply in (wrong, off, off_by_operator):
        same_reply(reply, unknown)
    # One bcrypt check on every path: the dummy one when no account is found
    # (disabled ones are not found), the real one for a wrong password.
    assert via_unknown == via_off == via_operator == [reseller_routes._DUMMY_HASH]
    assert via_wrong == [active.password_hash]


async def test_disabled_reseller_with_the_right_password_gets_no_token(env):
    """Negative: the correct password for a disabled account is never
    verified against the real hash, and returns no token."""
    disabled = await make_reseller(await make_operator(), is_active=False)
    res, checked = await sign_in(env, disabled.email)
    assert res.status_code == 401 and "access_token" not in res.text
    assert disabled.password_hash not in checked
