"""The one-time-code rules in admin_accounts/otp.py, pinned directly against a
REAL database.

The endpoint suites (test_admin_accounts_flow, test_admin_security_flow) reach
otp.py through onboarding, password reset, PIN reset and phone change, but
only a few of its rules are asserted there. This file pins every rule once,
through the admin call signature exactly as those routes use it, so a change
to otp.py is checked against the behaviour, not just against the routes that
happen to exercise it.

Runs only against a disposable database:

    ADMIN_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

FLOW_DB = os.getenv("ADMIN_FLOW_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs ADMIN_FLOW_TEST_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    from sqlalchemy import select

    from src.db.base import async_session_factory
    from src.db.models import AdminOtpCode, AdminUser, ISPOperator
    from src.modules.admin_accounts import otp as otp_service
    from src.utils.auth import hash_password

PHONE = "233244000333"


async def make_admin():
    slug = f"otp-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Otp {slug}", slug=slug, contact_email=f"{slug}@throwaway.test", status="approved")
        db.add(operator)
        await db.flush()
        admin = AdminUser(
            isp_operator_id=operator.id, email=f"{slug}@throwaway.test", password_hash=hash_password("x"),
            role="superadmin", is_active=True, phone=PHONE, phone_verified=True,
        )
        db.add(admin)
        await db.commit()
        return admin.id


async def issue(admin_id, purpose="reset", phone=PHONE):
    async with async_session_factory() as db:
        row, code = await otp_service.issue_code(db, admin_user_id=admin_id, purpose=purpose, phone=phone)
        await db.commit()
        return row.id, code


async def verify(admin_id, code, purpose="reset", *, commit=True):
    async with async_session_factory() as db:
        result = await otp_service.verify_code(db, admin_user_id=admin_id, purpose=purpose, code=code)
        if commit:
            await db.commit()
        else:
            await db.rollback()
        return result


async def code_row(row_id):
    async with async_session_factory() as db:
        return await db.get(AdminOtpCode, row_id)


def other(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


async def test_code_is_six_digits_and_stored_only_as_a_bcrypt_hash():
    admin_id = await make_admin()
    row_id, code = await issue(admin_id)
    assert len(code) == 6 and code.isdigit()
    row = await code_row(row_id)
    assert row.code_hash != code and row.code_hash.startswith("$2b$")
    assert code not in row.code_hash
    assert row.phone == PHONE and row.attempt_count == 0 and row.consumed_at is None
    ttl = row.expires_at - row.created_at
    assert timedelta(minutes=9) < ttl <= timedelta(minutes=10, seconds=5)


async def test_correct_code_verifies_once_and_is_consumed():
    admin_id = await make_admin()
    row_id, code = await issue(admin_id)
    result = await verify(admin_id, code)
    assert result.ok and result.row.id == row_id and result.reason is None
    assert (await code_row(row_id)).consumed_at is not None

    again = await verify(admin_id, code)
    assert not again.ok and again.reason == "no_code"


async def test_surrounding_whitespace_is_ignored():
    admin_id = await make_admin()
    _, code = await issue(admin_id)
    assert (await verify(admin_id, f"  {code} ")).ok


async def test_wrong_code_counts_and_the_count_survives_a_caller_rollback():
    admin_id = await make_admin()
    row_id, code = await issue(admin_id)
    result = await verify(admin_id, other(code), commit=False)  # caller rolls back
    assert not result.ok and result.reason == "mismatch"
    assert (await code_row(row_id)).attempt_count == 1


async def test_malformed_codes_count_as_attempts():
    admin_id = await make_admin()
    row_id, _ = await issue(admin_id)
    for bad in ("12345", "1234567", "abcdef", ""):
        assert (await verify(admin_id, bad)).reason in ("mismatch", "locked")
    assert (await code_row(row_id)).attempt_count == 4


async def test_fifth_miss_locks_and_the_right_code_is_then_refused():
    admin_id = await make_admin()
    row_id, code = await issue(admin_id)
    reasons = [(await verify(admin_id, other(code))).reason for _ in range(otp_service.OTP_MAX_ATTEMPTS)]
    assert otp_service.OTP_MAX_ATTEMPTS == 5
    assert reasons == ["mismatch"] * 4 + ["locked"]
    after = await verify(admin_id, code)
    assert not after.ok and after.reason == "locked"
    assert (await code_row(row_id)).attempt_count == 5, "a locked code must not keep counting"
    assert (await code_row(row_id)).consumed_at is None


async def test_expired_code_is_refused_even_when_correct():
    admin_id = await make_admin()
    row_id, code = await issue(admin_id)
    async with async_session_factory() as db:
        row = await db.get(AdminOtpCode, row_id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
    result = await verify(admin_id, code)
    assert not result.ok and result.reason == "expired"


async def test_a_new_code_supersedes_the_open_one_for_the_same_purpose():
    admin_id = await make_admin()
    old_id, old_code = await issue(admin_id)
    new_id, new_code = await issue(admin_id)
    assert (await code_row(old_id)).consumed_at is not None
    if old_code != new_code:  # 1-in-a-million collision would make this vacuous
        assert not (await verify(admin_id, old_code)).ok
    assert (await verify(admin_id, new_code)).ok


async def test_purposes_are_independent():
    admin_id = await make_admin()
    reset_id, reset_code = await issue(admin_id, "reset")
    pin_id, pin_code = await issue(admin_id, "pin_reset")
    # Issuing the pin_reset code must not void the open reset code...
    assert (await code_row(reset_id)).consumed_at is None
    # ...and a reset code must not satisfy a pin_reset check, or vice versa.
    if reset_code != pin_code:
        assert not (await verify(admin_id, reset_code, "pin_reset")).ok
    assert (await verify(admin_id, reset_code, "reset")).ok
    assert (await verify(admin_id, pin_code, "pin_reset")).ok


async def test_no_code_issued_reports_no_code():
    admin_id = await make_admin()
    result = await verify(admin_id, "123456", "phone_change")
    assert not result.ok and result.reason == "no_code" and result.row is None


async def test_codes_are_scoped_to_their_admin():
    a, b = await make_admin(), await make_admin()
    _, code = await issue(a)
    assert (await verify(b, code)).reason == "no_code"
    assert (await verify(a, code)).ok


async def test_failure_messages():
    fm = otp_service.failure_message
    assert fm(otp_service.VerifyResult(False, reason="locked")).startswith("Too many incorrect attempts")
    assert fm(otp_service.VerifyResult(False, reason="expired")).startswith("This code has expired")
    assert fm(otp_service.VerifyResult(False, reason="no_code")).startswith("This code has expired")
    assert fm(otp_service.VerifyResult(False, reason="mismatch")).startswith("Incorrect code")
