"""What an audit row may say: modules.audit.redact and the row builder.

Pure unit tests: no server, no DB, no Redis.
"""
from __future__ import annotations

import datetime
import decimal
import enum
import uuid

import pytest
from sqlalchemy import inspect as sa_inspect

from src.db import models
from src.modules.audit import Actor, RequestInfo
from src.modules.audit.context import request_info_from, _valid_ip
from src.modules.audit.redact import CHANGED, REDACTED, diff, is_secret_name, jsonable, redact_detail, snapshot
from src.modules.audit.service import build


@pytest.mark.parametrize("name", [
    "password", "password_hash", "new_password", "temp_password", "current_password",
    "pin", "pin_hash", "new_pin", "current_pin", "pinHash",
    "nas_secret", "nas_secret_plain", "client_secret", "secret_key", "webhook_secret",
    "api_key", "apiKey", "public_key", "private_key", "encryption_key",
    "access_token", "refresh_token", "token",
    "code", "code_hash", "otp", "otp_code",
    "security_answer_hash", "answer", "challenge_hashes", "challenge_pending_jti",
    "salt", "digests", "credentials", "authorization", "signature",
    "secret_encrypted", "api_key_encrypted", "ciphertext",
])
def test_secret_names_are_recognised(name):
    assert is_secret_name(name)


@pytest.mark.parametrize("name", [
    "name", "email", "phone", "price_ghs", "duration_minutes", "is_active", "role",
    "client_ip", "via", "attempts", "locked_for_hours", "old_phone", "new_phone",
    "status", "primary_color", "slug", "provider", "reason", "spinner",
])
def test_ordinary_names_are_not(name):
    assert not is_secret_name(name)


def test_every_credential_column_in_the_schema_is_caught():
    """Walks every mapped column: anything that stores a secret must be caught
    by name, so a diff can never print it even when a caller lists it."""
    must_hide = {
        "password_hash", "pin_hash", "security_answer_hash", "challenge_hashes", "challenge_pending_jti",
        "code_hash", "nas_secret", "nas_secret_plain",
    }
    columns = set()
    for mapper in models.Base.registry.mappers:
        columns |= {c.key for c in sa_inspect(mapper.class_).columns}
    secret_like = {c for c in columns if any(p in c for p in ("secret", "password", "hash", "_key", "token"))}
    assert must_hide <= columns
    missed = sorted(c for c in secret_like | must_hide if not is_secret_name(c))
    assert not missed


def test_diff_shows_only_listed_fields():
    before = {"name": "Day", "price_ghs": decimal.Decimal("5.00"), "description": "old"}
    after = {"name": "Day pass", "price_ghs": decimal.Decimal("5.00"), "description": "new"}
    assert diff(before, after, shown=["name"]) == {
        "name": {"before": "Day", "after": "Day pass"},
        "description": CHANGED,
    }


def test_secret_wins_over_shown():
    changes = diff({"api_key": "sk_live_old", "pin_hash": "a"}, {"api_key": "sk_live_new", "pin_hash": "b"},
                   shown=["api_key", "pin_hash"])
    assert changes == {"api_key": CHANGED, "pin_hash": CHANGED}
    assert "sk_live" not in repr(changes)


def test_diff_of_nothing_is_none_and_create_delete_work():
    assert diff({"a": 1}, {"a": 1}, shown=["a"]) is None
    assert diff(None, {"a": 1}, shown=["a"]) == {"a": {"before": None, "after": 1}}
    assert diff({"a": 1}, None) == {"a": CHANGED}


def test_redact_detail_is_recursive():
    detail = {
        "via": "access",
        "nested": {"refresh_token": "eyJ...", "items": [{"pin": "1234", "ok": 1}]},
        "otp": "123456",
    }
    assert redact_detail(detail) == {
        "via": "access",
        "nested": {"refresh_token": REDACTED, "items": [{"pin": REDACTED, "ok": 1}]},
        "otp": REDACTED,
    }


def test_redact_detail_caps_depth_and_length():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}
    assert "1" not in repr(redact_detail(deep))
    assert len(redact_detail({"x": "y" * 2000})["x"]) <= 501


def test_jsonable():
    class Colour(enum.Enum):
        red = "red"

    uid = uuid.uuid4()
    when = datetime.datetime(2026, 10, 5, tzinfo=datetime.timezone.utc)
    assert jsonable(uid) == str(uid)
    assert jsonable(when) == when.isoformat()
    assert jsonable(decimal.Decimal("1.50")) == "1.50"
    assert jsonable(Colour.red) == "red"
    assert jsonable(True) is True and jsonable(None) is None


def test_snapshot_reads_attributes():
    class Obj:
        name = "x"
        price = 2

    assert snapshot(Obj(), ["name", "price", "missing"]) == {"name": "x", "price": 2, "missing": None}


def test_build_redacts_even_hand_made_changes():
    row = build("x.update", actor=Actor.system(), changes={"nas_secret": {"before": "a", "after": "b"}},
                detail={"api_key": "k", "note": "fine"})
    assert row.changes == {"nas_secret": REDACTED}
    assert row.detail == {"api_key": REDACTED, "note": "fine"}


def test_build_tenant_defaults_to_the_actor_and_can_be_cleared():
    op = uuid.uuid4()
    actor = Actor("operator_user", uuid.uuid4(), "a@b.test", op)
    assert build("a", actor=actor).isp_operator_id == op
    assert build("a", actor=actor, isp_operator_id=None).isp_operator_id is None
    owner = Actor("platform_owner", uuid.uuid4(), "o@b.test")
    other = uuid.uuid4()
    assert build("a", actor=owner).isp_operator_id is None
    assert build("a", actor=owner, isp_operator_id=other).isp_operator_id == other


def test_build_without_context_is_a_system_row():
    row = build("job.ran")
    assert (row.actor_type, row.actor_id, row.ip) == ("system", None, None)


def test_build_takes_explicit_request_info():
    row = build("a", actor=Actor.system(), request_info=RequestInfo("10.0.0.1", "ua", "rid"), target_id=uuid.UUID(int=1))
    assert (row.ip, row.user_agent, row.request_id) == ("10.0.0.1", "ua", "rid")
    assert row.target_id == str(uuid.UUID(int=1))


@pytest.mark.parametrize("value,expected", [
    ("192.0.2.4", "192.0.2.4"), ("2001:db8::1", "2001:db8::1"), ("testclient", None), ("", None), (None, None),
])
def test_ip_must_be_an_address(value, expected):
    assert _valid_ip(value) == expected


def test_request_info_from_headers():
    class Client:
        host = "198.51.100.7"

    class Req:
        client = Client()
        headers = {"user-agent": "x" * 400, "x-request-id": "abc-123"}

    info = request_info_from(Req())
    assert info.ip == "198.51.100.7" and len(info.user_agent) == 256 and info.request_id == "abc-123"

    Req.headers = {"x-request-id": "bad id; drop table"}
    info = request_info_from(Req())
    assert info.request_id != "bad id; drop table" and len(info.request_id) == 32 and info.user_agent is None
