"""The support code the live-server integration suites rely on, checked without
a live server: the conftest safety guard, the shared challenge-answering path
(tests/platform_owner_session.py), and test_multi_tenancy's skip when the
seeded owner's code isn't provided.

The integration suites themselves can't run here (they need a separate
throwaway server), so this pins everything they depend on that can be tested
in isolation. Parts that create an owner need a disposable database:

    PLATFORM_OWNER_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import conftest
from platform_owner_session import (
    SEED_CODE_VAR,
    challenge_characters,
    create_ready_owner,
    owner_login_via_request,
    seeded_owner_code,
)

BACKEND = Path(__file__).resolve().parents[1]
FLOW_DB = os.getenv("PLATFORM_OWNER_FLOW_TEST_DATABASE_URL", "")
DB_OK = bool(FLOW_DB) and "throwaway" in FLOW_DB.rsplit("/", 1)[-1] and os.getenv("DATABASE_URL") == FLOW_DB
needs_db = pytest.mark.skipif(not DB_OK, reason="needs a throwaway PLATFORM_OWNER_FLOW_TEST_DATABASE_URL == DATABASE_URL")

SAFE_BASE = "http://localhost:9"  # nothing listens here: nothing can be written
THROWAWAY_DB = "postgresql+asyncpg://u:p@127.0.0.1:5432/ci_throwaway"
PROD_LIKE_DB = "postgresql+asyncpg://hotspot_user:x@127.0.0.1:6432/hotspot_db"


# ── Guard rules ───────────────────────────────────────────────────────────


@pytest.fixture
def opted_in(monkeypatch):
    monkeypatch.setenv("ALLOW_INTEGRATION_TESTS", "1")


@pytest.mark.parametrize("url,name", [
    (THROWAWAY_DB, "ci_throwaway"),
    ("postgresql://u:p@h:5432/security_throwaway?sslmode=disable", "security_throwaway"),
    (PROD_LIKE_DB, "hotspot_db"),
    ("postgresql://u:p@h:5432/", ""),
    ("", ""),
])
def test_database_name_parsing(url, name):
    assert conftest._database_name(url) == name


def test_a_throwaway_database_passes(opted_in):
    assert conftest._guard_failures(SAFE_BASE, THROWAWAY_DB) == []


@pytest.mark.parametrize("url", [PROD_LIKE_DB, "", "postgresql://u:p@h:5432/",
                                 # "throwaway" outside the database name must not count
                                 "postgresql://throwaway:p@h:5432/hotspot_db?application_name=throwaway"])
def test_anything_but_a_throwaway_database_is_refused(opted_in, url):
    failures = conftest._guard_failures(SAFE_BASE, url)
    assert any("DATABASE_URL" in f for f in failures), failures


def test_unset_database_url_is_refused(opted_in, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert any("unset" in f for f in conftest._guard_failures(SAFE_BASE))


def test_the_database_check_is_independent_of_the_other_two(monkeypatch):
    """A prod-like DATABASE_URL is refused even when opt-in and base URL are
    fine, and a throwaway one doesn't excuse a missing opt-in."""
    monkeypatch.setenv("ALLOW_INTEGRATION_TESTS", "1")
    assert len(conftest._guard_failures(SAFE_BASE, PROD_LIKE_DB)) == 1
    monkeypatch.delenv("ALLOW_INTEGRATION_TESTS")
    assert len(conftest._guard_failures(SAFE_BASE, THROWAWAY_DB)) == 1


def collect(module: str, **env) -> subprocess.CompletedProcess:
    full = {k: v for k, v in os.environ.items() if k not in ("ALLOW_INTEGRATION_TESTS", "TEST_BASE_URL")}
    full.update({"PYTHONDONTWRITEBYTECODE": "1", **env})
    return subprocess.run([sys.executable, "-m", "pytest", f"tests/{module}", "--collect-only", "-q", "-p", "no:cacheprovider"],
                          cwd=BACKEND, env=full, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("module", ["test_billing.py", "test_multi_tenancy.py"])
def test_real_collection_is_blocked_on_a_prod_like_database(module):
    res = collect(module, ALLOW_INTEGRATION_TESTS="1", TEST_BASE_URL=SAFE_BASE, DATABASE_URL=PROD_LIKE_DB)
    out = res.stdout + res.stderr
    assert res.returncode == 2, out
    assert "INTEGRATION TESTS BLOCKED" in out and "DATABASE_URL does not name a throwaway database" in out


@pytest.mark.parametrize("module,count", [("test_billing.py", 10), ("test_multi_tenancy.py", 15)])
def test_both_suites_import_and_collect_on_a_throwaway_database(module, count):
    res = collect(module, ALLOW_INTEGRATION_TESTS="1", TEST_BASE_URL=SAFE_BASE, DATABASE_URL=THROWAWAY_DB)
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert f"{count} tests collected" in out, out


# ── Shared challenge-answering path ───────────────────────────────────────


def test_challenge_characters():
    assert challenge_characters("ABCDEFGHJK23", [1, 6, 12]) == {"1": "A", "6": "F", "12": "3"}


class FakeServer:
    """Records calls; answers from a script of (status, body) responses."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, path, body=None, **_):
        self.calls.append((method, path, body))
        return self.responses.pop(0)


def test_via_request_answers_the_challenge_with_the_right_characters():
    server = FakeServer((200, {"challenge_required": True, "challenge_token": "tok", "positions": [2, 5, 9]}),
                        (200, {"access_token": "a", "refresh_token": "r"}))
    status, body = owner_login_via_request(server, "o@ci.test", "pw", "ABCDEFGHJK23")
    assert (status, body["access_token"]) == (200, "a")
    assert server.calls == [
        ("POST", "/api/v1/platform/auth/login", {"email": "o@ci.test", "password": "pw"}),
        ("POST", "/api/v1/platform/auth/challenge", {"challenge_token": "tok", "characters": {"2": "B", "5": "E", "9": "J"}}),
    ]


@pytest.mark.parametrize("first", [(401, {"detail": "Invalid email or password"}),
                                   (403, {"detail": "locked"}),
                                   (200, {"access_token": "a", "refresh_token": "r"})])
def test_via_request_returns_non_challenge_responses_untouched(first):
    server = FakeServer(first)
    assert owner_login_via_request(server, "o@ci.test", "pw", "ABCDEFGHJK23") == first
    assert len(server.calls) == 1, "no challenge call unless a challenge was issued"


def test_seeded_owner_code_reads_the_seed_variable(monkeypatch):
    monkeypatch.delenv(SEED_CODE_VAR, raising=False)
    assert seeded_owner_code() is None
    monkeypatch.setenv(SEED_CODE_VAR, " abcdefghjk23 ")
    assert seeded_owner_code() == "ABCDEFGHJK23"


@needs_db
def test_create_ready_owner_is_the_full_completed_state_and_signs_in_for_real(monkeypatch):
    """The billing suite's path end to end, minus the network: a ready owner,
    then owner_login_via_request against the real app (in-process)."""
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from src.app import app
    from src.db.base import async_session_factory, engine
    from src.db.models import PlatformOwner
    from src.modules.platform import routes as platform_routes

    async def no_limit(*a, **k):
        return None
    monkeypatch.setattr(platform_routes, "enforce_rate_limit", no_limit)

    email, password = f"support-{uuid.uuid4().hex[:10]}@ci.test", "Support-Passw0rd"

    async def make_and_read():
        code = await create_ready_owner(email, password)
        async with async_session_factory() as db:
            owner = (await db.execute(select(PlatformOwner).where(PlatformOwner.email == email))).scalar_one()
        await engine.dispose()
        return code, owner
    code, owner = asyncio.run(make_and_read())
    assert owner.must_complete_security_setup is False
    assert owner.phone_verified is True and owner.phone
    assert owner.security_question and owner.security_answer_hash
    assert owner.challenge_hashes and owner.challenge_hashes.get("confirmed_at")

    with TestClient(app) as client:
        def request(method, path, body=None, **_):
            res = client.request(method, path, json=body)
            return res.status_code, res.json()
        status, body = owner_login_via_request(request, email, password, code)
        assert status == 200 and "access_token" in body, body
        me = client.get("/api/v1/platform/me", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert me.status_code == 200, "the setup gate must be open for a ready owner"
    engine.sync_engine.dispose()


# ── test_multi_tenancy's skip ─────────────────────────────────────────────


OWNER_TESTS = [
    "test_platform_owner_can_see_operator_summary_and_admin_cannot",
    "test_platform_owner_token_cannot_access_admin_endpoints",
    "test_sms_credentials_crud_roundtrip_and_redaction",
    "test_operator_creation_scopes_initial_admin_to_new_operator",
]


def test_multi_tenancy_owner_tests_skip_with_a_message_when_the_code_is_missing():
    """Actually runs them: with no SEED_OWNER_CHARACTER_CODE they must skip
    before any HTTP (nothing listens on the target, so any request would
    error, not skip)."""
    env = {k: v for k, v in os.environ.items() if k != SEED_CODE_VAR}
    env.update(ALLOW_INTEGRATION_TESTS="1", TEST_BASE_URL=SAFE_BASE, DATABASE_URL=THROWAWAY_DB, PYTHONDONTWRITEBYTECODE="1")
    res = subprocess.run([sys.executable, "-m", "pytest", "tests/test_multi_tenancy.py", "-rs", "-q", "-p", "no:cacheprovider",
                          "-k", " or ".join(OWNER_TESTS)], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120)
    out = res.stdout + res.stderr
    assert res.returncode == 0, out  # all skipped: pytest exits 0; any failure or error would not
    assert f"{len(OWNER_TESTS)} skipped" in out and "passed" not in out, out
    assert f"{SEED_CODE_VAR} is not set" in out
