"""Unit tests for the public operator-application flow.

Covers: email availability scoping (pending/approved/admin hold an email,
rejected releases it), non-enumerable responses, the check endpoint's per-IP
rate limit, the business-description minimum, and the approve-time guard
against an existing admin email.

No server and no database: sessions are faked, rate limiting and notifications
are monkeypatched. Safe to run anywhere, including the production container.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from src.db.base import get_db
from src.middleware.auth import get_platform_owner_context
from src.modules.applications import routes, service
from src.modules.applications.schemas import (
    MESSAGE_MAX_LENGTH,
    MESSAGE_MIN_LENGTH,
    ApplicationSubmit,
    EmailCheckRequest,
)

VALID_APPLICATION = {
    "isp_name": "AccraNet ISP",
    "contact_name": "Kwame Mensah",
    "email": "kwame@accranet.test",
    "phone": "0244123456",
    "region": "Greater Accra",
    "message": "Three hotspot sites around Madina market.",
}


class FakeResult:
    def __init__(self, row=None, scalar=None):
        self._row = row
        self._scalar = scalar

    def one(self):
        return self._row

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeDb:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []
        self.added = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        obj.id = obj.id or uuid.uuid4()


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _match_row(admin: bool, application: bool) -> FakeResult:
    return FakeResult(row=SimpleNamespace(admin_match=admin, application_match=application))


# ── Schema: business description & email normalisation ───────────────────────

def test_description_is_required():
    body = {k: v for k, v in VALID_APPLICATION.items() if k != "message"}
    with pytest.raises(ValidationError):
        ApplicationSubmit(**body)


@pytest.mark.parametrize("message", ["", "Short", "x" * (MESSAGE_MIN_LENGTH - 1)])
def test_description_below_minimum_gets_friendly_prompt(message):
    with pytest.raises(ValidationError) as exc:
        ApplicationSubmit(**{**VALID_APPLICATION, "message": message})
    assert f"at least {MESSAGE_MIN_LENGTH} characters" in str(exc.value)


def test_description_whitespace_padding_does_not_count():
    padded = "   " + "x" * (MESSAGE_MIN_LENGTH - 1) + "\n\n   "
    with pytest.raises(ValidationError):
        ApplicationSubmit(**{**VALID_APPLICATION, "message": padded})


def test_description_at_minimum_is_accepted_and_trimmed():
    body = ApplicationSubmit(**{**VALID_APPLICATION, "message": "  " + "x" * MESSAGE_MIN_LENGTH + "  "})
    assert body.message == "x" * MESSAGE_MIN_LENGTH


def test_description_over_maximum_is_rejected():
    with pytest.raises(ValidationError):
        ApplicationSubmit(**{**VALID_APPLICATION, "message": "x" * (MESSAGE_MAX_LENGTH + 1)})


@pytest.mark.parametrize("model", [ApplicationSubmit, EmailCheckRequest])
def test_email_is_trimmed_and_lowercased(model):
    extra = VALID_APPLICATION if model is ApplicationSubmit else {}
    assert model(**{**extra, "email": "  Kwame@AccraNet.TEST "}).email == "kwame@accranet.test"


def test_email_check_rejects_malformed_email():
    with pytest.raises(ValidationError):
        EmailCheckRequest(email="not-an-email")


# ── Availability query: scoping ────────────────────────────────────────────────

def test_rejected_applications_do_not_hold_an_email():
    assert set(service.EMAIL_HOLDING_APPLICATION_STATUSES) == {"pending", "approved"}


def test_availability_query_is_case_insensitive_and_scoped():
    sql = _sql(service._email_in_use_stmt("  Kwame@AccraNet.TEST "))
    assert "lower(admin_users.email) = 'kwame@accranet.test'" in sql
    assert "lower(operator_applications.email) = 'kwame@accranet.test'" in sql
    assert "operator_applications.status IN ('pending', 'approved')" in sql
    assert "rejected" not in sql


def test_availability_query_evaluates_both_sources_as_separate_columns():
    # Two EXISTS columns, not one OR: both always run, so timing doesn't leak which matched.
    sql = _sql(service._email_in_use_stmt("a@b.test"))
    assert sql.count("EXISTS") == 2
    assert " OR " not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin, application, expected",
    [(False, False, False), (True, False, True), (False, True, True), (True, True, True)],
)
async def test_email_in_use_combines_both_matches(admin, application, expected):
    db = FakeDb(_match_row(admin, application))
    assert await service.email_in_use(db, "a@b.test") is expected
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_admin_email_exists_query_is_case_insensitive():
    db = FakeDb(FakeResult(scalar=True))
    assert await service.admin_email_exists(db, " Owner@Example.TEST") is True
    assert "lower(admin_users.email) = 'owner@example.test'" in _sql(db.statements[0])


# ── Service: submit ───────────────────────────────────────────────────────────

@pytest.fixture
def notifications(monkeypatch):
    sent = []

    async def fake_notify(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(service.notify, "notify_application_received", fake_notify)
    return sent


@pytest.mark.asyncio
async def test_submit_refuses_held_email_before_writing_or_notifying(monkeypatch, notifications):
    async def held(db, email):
        return True

    monkeypatch.setattr(service, "email_in_use", held)
    db = FakeDb()
    with pytest.raises(service.EmailUnavailableError):
        await service.submit_application(db, ApplicationSubmit(**VALID_APPLICATION))
    assert db.added == [] and db.commits == 0 and notifications == []


@pytest.mark.asyncio
async def test_submit_proceeds_when_email_is_free(notifications):
    # (False, False) is what a previously *rejected* applicant's email returns:
    # the query excludes rejected applications, so the submit goes through.
    db = FakeDb(_match_row(False, False))
    app = await service.submit_application(db, ApplicationSubmit(**VALID_APPLICATION))
    assert app.status == "pending" and app.email == "kwame@accranet.test"
    assert len(db.added) == 1 and db.commits == 1 and len(notifications) == 1


# ── Routes ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch):
    db = FakeDb()
    app = FastAPI()
    app.include_router(routes.public_router, prefix="/api/v1")
    app.include_router(routes.platform_router, prefix="/api/v1")

    async def fake_get_db():
        yield db

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_platform_owner_context] = lambda: SimpleNamespace(id=uuid.uuid4())

    rate_limit_calls = []

    async def fake_rate_limit(ip, bucket, limit=10, window_seconds=60):
        rate_limit_calls.append({"bucket": bucket, "limit": limit, "window_seconds": window_seconds})

    monkeypatch.setattr(routes, "enforce_rate_limit", fake_rate_limit)
    return SimpleNamespace(client=TestClient(app), db=db, rate_limit_calls=rate_limit_calls)


def test_check_email_is_rate_limited_20_per_minute_per_ip(api):
    api.db.results.append(_match_row(False, False))
    res = api.client.post("/api/v1/public/apply/check-email", json={"email": "new@isp.test"})
    assert res.status_code == 200
    assert api.rate_limit_calls == [{"bucket": "public:apply-email-check", "limit": 20, "window_seconds": 60}]


def test_check_email_rate_limit_blocks_before_touching_the_db(api, monkeypatch):
    async def limited(*args, **kwargs):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    monkeypatch.setattr(routes, "enforce_rate_limit", limited)
    res = api.client.post("/api/v1/public/apply/check-email", json={"email": "new@isp.test"})
    assert res.status_code == 429
    assert api.db.statements == []


def test_check_email_free_address(api):
    api.db.results.append(_match_row(False, False))
    res = api.client.post("/api/v1/public/apply/check-email", json={"email": "New@ISP.test"})
    assert res.json() == {"available": True}


def test_check_email_responses_are_identical_whatever_matched(api):
    # Admin match vs. pending/approved application match: byte-identical responses.
    bodies = []
    for row in (_match_row(True, False), _match_row(False, True), _match_row(True, True)):
        api.db.results.append(row)
        res = api.client.post("/api/v1/public/apply/check-email", json={"email": "held@isp.test"})
        assert res.status_code == 200
        bodies.append(res.content)
    assert bodies == [b'{"available":false}'] * 3


def test_check_email_malformed_is_422(api):
    res = api.client.post("/api/v1/public/apply/check-email", json={"email": "nope"})
    assert res.status_code == 422


def test_apply_with_held_email_returns_generic_409_and_writes_nothing(api, notifications):
    for row in (_match_row(True, False), _match_row(False, True)):
        api.db.results.append(row)
        res = api.client.post("/api/v1/public/apply", json=VALID_APPLICATION)
        assert res.status_code == 409
        assert res.json() == {"detail": service.EMAIL_UNAVAILABLE_MESSAGE}
    assert api.db.added == [] and notifications == []


def test_generic_message_names_no_record_type():
    lowered = service.EMAIL_UNAVAILABLE_MESSAGE.lower()
    for leak in ("admin", "pending", "approved", "rejected", "operator", "exists"):
        assert leak not in lowered


def test_apply_with_short_description_is_422_on_message(api):
    res = api.client.post("/api/v1/public/apply", json={**VALID_APPLICATION, "message": "hi"})
    assert res.status_code == 422
    assert res.json()["detail"][0]["loc"][-1] == "message"


def test_approve_is_409_not_500_when_admin_email_exists(api, monkeypatch):
    pending = SimpleNamespace(id=uuid.uuid4(), status="pending", email="taken@isp.test")
    api.db.results.append(FakeResult(scalar=pending))

    async def exists(db, email):
        return True

    async def must_not_run(*args, **kwargs):
        raise AssertionError("approve_application must not run when the admin email exists")

    monkeypatch.setattr(service, "admin_email_exists", exists)
    monkeypatch.setattr(service, "approve_application", must_not_run)
    res = api.client.put(f"/api/v1/platform/applications/{pending.id}/approve")
    assert res.status_code == 409
    assert "admin account with this email already exists" in res.json()["detail"]
