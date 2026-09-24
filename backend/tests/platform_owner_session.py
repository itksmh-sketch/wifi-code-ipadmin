"""Test helper: a platform owner who has completed security setup, and the
two-step sign-in (password, then the character challenge) such an owner goes
through.

Every platform-owner route is gated on setup being complete, so a suite that
needs an owner session creates the owner with create_ready_owner rather than a
bare PlatformOwner(...) (whose default, like production's, is setup pending).
It stores a real character code and the suites sign in through the real
challenge, rather than minting tokens, so they keep exercising the login path.

Two request styles share one challenge-answering path:
  * owner_login             — an httpx AsyncClient whose base_url ends in /api/v1
                              (the in-process flow suites);
  * owner_login_via_request — a ``request(method, path, body=...) -> (status,
                              body)`` function (the live-server integration
                              suites' urllib ``_request``).

Not a test module (no test_ prefix). Its app imports are deferred so importing
it never binds the database engine before a suite's own guard has run.
"""
from __future__ import annotations

import os

LOGIN_PATH = "/platform/auth/login"
CHALLENGE_PATH = "/platform/auth/challenge"
SEED_CODE_VAR = "SEED_OWNER_CHARACTER_CODE"


def challenge_characters(code: str, positions: list[int]) -> dict[str, str]:
    """The answer to a challenge: {"<1-based position>": character}."""
    return {str(p): code[p - 1] for p in positions}


def is_challenge(status: int, body) -> bool:
    return status == 200 and isinstance(body, dict) and body.get("challenge_required") is True


def challenge_answer_body(code: str, challenge: dict) -> dict:
    return {"challenge_token": challenge["challenge_token"],
            "characters": challenge_characters(code, challenge["positions"])}


async def create_ready_owner(email: str, password: str, **values) -> str:
    """Insert an active owner in the completed-setup state the wizard produces
    (verified phone, security question, code stored and confirmed, gate off).
    Returns the plaintext code, for owner_login / owner_login_via_request."""
    from src.db.base import async_session_factory
    from src.db.models import PlatformOwner
    from src.modules.platform import character_code
    from src.modules.platform.owner_setup import mark_setup_complete
    from src.utils.auth import hash_password

    setup = {k: values.pop(k) for k in ("phone", "security_question", "security_answer") if k in values}
    async with async_session_factory() as db:
        owner = PlatformOwner(email=email, password_hash=hash_password(password),
                              name=values.pop("name", "Owner"), is_active=True, **values)
        db.add(owner)
        await db.flush()
        code = character_code.generate_code()
        mark_setup_complete(owner, code=code, **setup)
        await db.commit()
    return code


async def owner_login(client, email: str, password: str, code: str):
    """httpx style. Returns the final response: the token pair on success, or
    whichever step refused."""
    res = await client.post(LOGIN_PATH, json={"email": email, "password": password})
    if not is_challenge(res.status_code, res.json() if res.status_code == 200 else None):
        return res
    return await client.post(CHALLENGE_PATH, json=challenge_answer_body(code, res.json()))


def owner_login_via_request(request, email: str, password: str, code: str, *, prefix: str = "/api/v1"):
    """urllib ``_request`` style: ``request(method, path, body=...) -> (status,
    body)``. Returns the final (status, body)."""
    status, body = request("POST", prefix + LOGIN_PATH, body={"email": email, "password": password})
    if not is_challenge(status, body):
        return status, body
    return request("POST", prefix + CHALLENGE_PATH, body=challenge_answer_body(code, body))


def seeded_owner_code() -> str | None:
    """The seeded owner's code, from the same variable the seed takes
    (SEED_OWNER_CHARACTER_CODE), or None if it isn't set."""
    code = os.getenv(SEED_CODE_VAR, "").strip().upper()
    return code or None
