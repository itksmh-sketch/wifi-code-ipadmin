"""Unit tests for operator-admin provisioning, onboarding gate and reset tokens.

No server and no database: the database is faked where one is touched. Safe to
run anywhere, including the production container. The full HTTP flows against a
real (throwaway) Postgres are covered by test_admin_accounts_flow.py.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.middleware import auth as auth_mw
from src.modules.admin_accounts import provisioning
from src.modules.admin_accounts.passwords import PASSWORD_POLICY_MESSAGE, password_policy_error
from src.modules.admin_accounts.security_questions import SECURITY_QUESTIONS, decoy_question_key, normalize_answer
from src.utils import auth as auth_utils
from src.utils.phone import mask_phone, normalize_ghana_phone
from src.utils.email_address import normalize_email


@pytest.mark.parametrize("pw,ok", [
    ("Abcdefg1", True),
    ("abcdefg1", False),   # no upper
    ("ABCDEFG1", False),   # no lower
    ("Abcdefgh", False),   # no digit
    ("Abc1", False),       # too short
    ("", False),
])
def test_password_policy(pw, ok):
    assert (password_policy_error(pw) is None) is ok
    if not ok:
        assert password_policy_error(pw) == PASSWORD_POLICY_MESSAGE


@pytest.mark.parametrize("raw,expected", [
    ("0244123456", "233244123456"),
    ("024 412 3456", "233244123456"),
    ("+233 24 412 3456", "233244123456"),
    ("233244123456", "233244123456"),
])
def test_phone_normalization(raw, expected):
    assert normalize_ghana_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "12345", "0144123456", "+44 7700 900123"])
def test_phone_normalization_rejects(raw):
    with pytest.raises(ValueError):
        normalize_ghana_phone(raw)


def test_mask_phone_hides_the_middle():
    assert mask_phone("233244123456") == "+233 •••• ••• 456"
    assert mask_phone(None) == ""


def test_email_normalization():
    assert normalize_email("  Admin@ISP.com ") == "admin@isp.com"
    with pytest.raises(ValueError):
        normalize_email("not-an-email")


def test_security_answer_normalization_and_decoy_is_stable():
    assert normalize_answer("  Accra   Academy ") == normalize_answer("accra academy")
    key = decoy_question_key("nobody@example.com")
    assert key in SECURITY_QUESTIONS
    assert decoy_question_key("NOBODY@example.com ") == key


# ── token_version ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("claim,db_value,ok", [
    (0, 0, True),
    (3, 3, True),
    (2, 3, False),     # password changed since the token was issued
    (None, 0, False),  # token minted before the claim existed
    (False, 0, False), # bool is not a version
    ("0", 0, False),
])
def test_token_version_matches(claim, db_value, ok):
    payload = {} if claim is None else {"tv": claim}
    assert auth_mw.token_version_matches(payload, SimpleNamespace(token_version=db_value)) is ok


def test_reset_grant_is_never_a_login_token_and_vice_versa():
    grant = auth_utils.create_password_reset_token(admin_id=str(uuid.uuid4()), token_version=4, via="otp")
    assert auth_utils.verify_token(grant) is None
    payload = auth_utils.verify_password_reset_token(grant)
    assert payload["tv"] == 4 and payload["via"] == "otp"

    access = auth_utils.create_access_token({"sub": "x", "isp_operator_id": "y", "tv": 0})
    assert auth_utils.verify_password_reset_token(access) is None
    assert auth_utils.verify_password_reset_token("garbage") is None


@pytest.mark.asyncio
async def test_onboarding_gate_blocks_everything_but_onboarding():
    pending = SimpleNamespace(must_complete_onboarding=True)
    with pytest.raises(HTTPException) as exc:
        await auth_mw.get_current_user(user=pending)
    assert exc.value.status_code == 403
    assert exc.value.headers == {auth_mw.ONBOARDING_REQUIRED_HEADER: "1"}

    changing = SimpleNamespace(must_complete_onboarding=False, must_change_password=True)
    with pytest.raises(HTTPException) as exc:
        await auth_mw.get_current_user(user=changing)
    assert exc.value.status_code == 403
    assert exc.value.headers == {auth_mw.ONBOARDING_REQUIRED_HEADER: "1"}

    done = SimpleNamespace(must_complete_onboarding=False, must_change_password=False)
    assert await auth_mw.get_current_user(user=done) is done


# ── provisioning ──────────────────────────────────────────────────────────

class FakeDb:
    def __init__(self, email_taken=False):
        self.email_taken = email_taken
        self.added = []
        self.flushed = False
        self.committed = False

    async def execute(self, statement):
        return SimpleNamespace(first=lambda: (uuid.uuid4(),) if self.email_taken else None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def commit(self):  # pragma: no cover - must never be called
        self.committed = True


@pytest.mark.asyncio
async def test_provision_sets_the_onboarding_gate_and_never_commits():
    db = FakeDb()
    operator_id = uuid.uuid4()
    admin, temp_password = await provisioning.provision_operator_admin(
        db, operator_id=operator_id, email="  New.Admin@ISP.com ", phone="024 412 3456", role="admin"
    )
    assert db.added == [admin] and db.flushed and not db.committed
    assert admin.email == "new.admin@isp.com"
    assert admin.phone == "233244123456"
    assert admin.isp_operator_id == operator_id
    assert admin.role == "admin" and admin.is_active is True
    assert admin.must_complete_onboarding is True
    assert admin.phone_verified is False
    assert admin.token_version == 0
    assert len(temp_password) == 12 and temp_password.isalnum()
    assert auth_utils.verify_password(temp_password, admin.password_hash)


@pytest.mark.asyncio
async def test_provision_refuses_a_taken_email():
    with pytest.raises(provisioning.AdminEmailInUseError):
        await provisioning.provision_operator_admin(
            FakeDb(email_taken=True), operator_id=uuid.uuid4(), email="a@b.co", phone="0244123456", role="admin"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"phone": "12345"},
    {"email": "nope"},
    {"role": "owner"},
])
async def test_provision_validates_inputs(kwargs):
    args = {"operator_id": uuid.uuid4(), "email": "a@b.co", "phone": "0244123456", "role": "admin", **kwargs}
    with pytest.raises(ValueError):
        await provisioning.provision_operator_admin(FakeDb(), **args)


def test_temp_passwords_are_unique():
    assert len({provisioning.generate_temp_password() for _ in range(50)}) == 50


# ── self-service payload guards (no DB) ───────────────────────────────────

from fastapi import HTTPException as _HTTPException  # noqa: E402  (grouped with the tests that use it)

from src.modules.platform.routes import (  # noqa: E402
    OperatorProfileUpdate,
    PlatformOwnerProfileUpdate,
    _parse_update,
    _reject_unknown_fields,
)

OPERATOR_FIELDS = {"name", "contact_email", "contact_phone"}


@pytest.mark.parametrize("payload", [
    {"name": "X"},
    {"contact_email": "a@b.co"},
    {"contact_phone": "0244123456"},
    {"name": "X", "contact_email": "a@b.co", "contact_phone": "0244123456"},
])
def test_allowlist_accepts_only_the_editable_fields(payload):
    _reject_unknown_fields(payload, OPERATOR_FIELDS)  # does not raise


@pytest.mark.parametrize("payload,named", [
    ({"slug": "new"}, "slug"),
    ({"monthly_fee_ghs": 1}, "monthly_fee_ghs"),
    ({"billing_status": "active"}, "billing_status"),
    ({"status": "suspended"}, "status"),
    ({"trial_ends_at": "2027-01-01"}, "trial_ends_at"),
    ({"id": "x"}, "id"),
    ({"credentials_encrypted": "x"}, "credentials_encrypted"),
    # A protected field hidden alongside a legitimate one still fails the whole call.
    ({"name": "OK", "slug": "sneaky"}, "slug"),
])
def test_allowlist_refuses_protected_fields_by_name(payload, named):
    with pytest.raises(_HTTPException) as exc:
        _reject_unknown_fields(payload, OPERATOR_FIELDS)
    assert exc.value.status_code == 400
    assert named in exc.value.detail


@pytest.mark.parametrize("payload", [{}, [], "not-a-dict", None])
def test_allowlist_refuses_empty_or_non_object_bodies(payload):
    with pytest.raises(_HTTPException) as exc:
        _reject_unknown_fields(payload, OPERATOR_FIELDS)
    assert exc.value.status_code == 400


def test_email_is_never_an_allowed_field_on_platform_me():
    with pytest.raises(_HTTPException) as exc:
        _reject_unknown_fields({"email": "new@x.co"}, {"name"})
    assert exc.value.status_code == 400 and "email" in exc.value.detail


@pytest.mark.parametrize("model,payload", [
    (PlatformOwnerProfileUpdate, {"name": ""}),
    (PlatformOwnerProfileUpdate, {"name": "x" * 256}),
    (PlatformOwnerProfileUpdate, {"name": 5}),
    (OperatorProfileUpdate, {"name": ""}),
    (OperatorProfileUpdate, {"contact_email": "x" * 300}),
    (OperatorProfileUpdate, {"contact_phone": "x" * 100}),
])
def test_field_errors_surface_as_400_not_500(model, payload):
    with pytest.raises(_HTTPException) as exc:
        _parse_update(model, payload)
    assert exc.value.status_code == 400 and exc.value.detail
