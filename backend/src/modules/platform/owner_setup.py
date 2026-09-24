"""Putting a platform owner straight into the "security setup complete" state,
for non-production use only: the seed's opt-in (SEED_OWNER_SETUP_COMPLETE)
and the test suites' ready-owner helper.

Real owners reach this state only through the setup wizard. This produces the
same end state the wizard's all-or-nothing completion checks for — verified
phone, security question, a stored and confirmed character code, gate off — so
nothing downstream can tell the difference.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from src.db.models import PlatformOwner
from src.modules.admin_accounts.security_questions import SECURITY_QUESTIONS, normalize_answer
from src.modules.platform import character_code
from src.utils.auth import hash_password

# Reserved-for-documentation TLDs and domains (RFC 2606 / RFC 6761), plus the
# ".local" mDNS suffix. An address outside these is treated as possibly real.
TEST_TLDS = {"test", "example", "invalid", "localhost", "local"}
TEST_DOMAINS = {"example.com", "example.org", "example.net"}

PLACEHOLDER_PHONE = "233200000000"
DEFAULT_QUESTION = "first_school"


def looks_like_test_email(email: str) -> bool:
    domain = (email or "").strip().lower().rpartition("@")[2]
    if not domain or "." not in domain:
        return False
    return domain in TEST_DOMAINS or domain.rsplit(".", 1)[1] in TEST_TLDS


def is_valid_code(code: str) -> bool:
    return (
        isinstance(code, str)
        and len(code) == character_code.CODE_LENGTH
        and all(c in character_code.ALPHABET for c in code)
    )


def mark_setup_complete(
    owner: PlatformOwner,
    *,
    code: str,
    phone: str = PLACEHOLDER_PHONE,
    security_question: str = DEFAULT_QUESTION,
    security_answer: str | None = None,
) -> None:
    """Set every field the wizard's completion requires. ``owner.id`` must
    already be assigned (flush first): the code's digests are bound to it.
    With no ``security_answer``, a random one is stored — nobody can answer it,
    which is fine for a seeded or test owner. Caller commits."""
    if owner.id is None:
        raise ValueError("flush the owner first: the code digests are bound to its id")
    if not is_valid_code(code):
        raise ValueError(
            f"a character code is {character_code.CODE_LENGTH} characters from {character_code.ALPHABET}"
        )
    if security_question not in SECURITY_QUESTIONS:
        raise ValueError(f"unknown security question {security_question!r}")

    storage = character_code.build_storage(owner.id, code)
    storage["confirmed_at"] = datetime.now(timezone.utc).isoformat()
    owner.phone = phone
    owner.phone_verified = True
    owner.security_question = security_question
    owner.security_answer_hash = hash_password(normalize_answer(security_answer or secrets.token_urlsafe(24)))
    owner.security_answer_attempt_count = 0
    owner.challenge_hashes = storage
    owner.challenge_set_at = datetime.now(timezone.utc)
    owner.challenge_pending_positions = None
    owner.challenge_pending_jti = None
    owner.must_complete_security_setup = False
