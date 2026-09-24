"""Test helper: a platform owner who has completed security setup, and the
two-step sign-in that such an owner goes through.

Every platform-owner route is gated on setup being complete, so a suite that
needs an owner session creates the owner with this rather than with a bare
PlatformOwner(...) (whose default, like production's, is setup pending).
It stores a real character code and signs in through the real challenge,
rather than minting tokens, so these suites keep exercising the login path.

Not a test module (no test_ prefix); imported by the flow suites. Its imports
are deferred so that importing it never binds the database engine before a
suite's throwaway-database guard has run.
"""
from __future__ import annotations


async def create_ready_owner(email: str, password: str, **values) -> str:
    """Insert an active owner with setup complete and a code on file.
    Returns the plaintext code (for owner_login)."""
    from src.db.base import async_session_factory
    from src.db.models import PlatformOwner
    from src.modules.platform import character_code
    from src.utils.auth import hash_password

    async with async_session_factory() as db:
        owner = PlatformOwner(
            email=email, password_hash=hash_password(password), name=values.pop("name", "Owner"),
            is_active=True, must_complete_security_setup=False,
            phone=values.pop("phone", "233244000999"), phone_verified=True, **values,
        )
        db.add(owner)
        await db.flush()
        code = character_code.generate_code()
        owner.challenge_hashes = character_code.build_storage(owner.id, code)
        await db.commit()
    return code


async def owner_login(client, email: str, password: str, code: str):
    """Password, then (if asked) the character challenge. Returns the final
    response: the token pair on success, or whichever step refused."""
    res = await client.post("/platform/auth/login", json={"email": email, "password": password})
    if res.status_code != 200 or not res.json().get("challenge_required"):
        return res
    body = res.json()
    characters = {str(p): code[p - 1] for p in body["positions"]}
    return await client.post(
        "/platform/auth/challenge", json={"challenge_token": body["challenge_token"], "characters": characters}
    )
