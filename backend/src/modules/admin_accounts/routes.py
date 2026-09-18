"""Operator-admin first-login onboarding and forgot-password endpoints.

Onboarding (authenticated, on a temp password):
    GET  /auth/onboarding/status
    POST /auth/onboarding/phone         send an OTP to the number to verify
    POST /auth/onboarding/otp/verify    confirm it -> phone_verified
    POST /auth/onboarding/password      new password + security question -> done

The same password endpoint serves an admin whose password a platform owner
reset (must_change_password): they skip the phone/OTP steps and the security
question is optional, since they already set one during onboarding.

Signed in:
    POST /auth/me/password              change your own password

Forgot password (anonymous, enumeration-safe):
    POST /auth/reset/request            SMS an OTP to the verified phone on file
    POST /auth/reset/verify-otp         OTP -> short-lived reset grant
    POST /auth/reset/security-question  question lookup, or answer -> reset grant
    POST /auth/reset/set-password       reset grant -> new password

Every password change bumps token_version, which invalidates every access and
refresh token the admin holds. Anonymous responses never reveal whether an
email belongs to an account: unknown emails get the same responses (and a
comparable amount of hashing work) as real ones.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import AdminOtpCode, AdminUser
from src.schemas import TokenResponse
from src.middleware.auth import get_authenticated_admin, get_current_user
from src.middleware.rate_limit import enforce_rate_limit
from src.modules.admin_accounts import otp as otp_service
from src.modules.admin_accounts.notifications import OTP_TTL_MINUTES, send_otp_sms
from src.modules.admin_accounts.passwords import password_policy_error
from src.modules.auth.tokens import admin_token_response
from src.modules.admin_accounts.security_questions import (
    ANSWER_MIN_LENGTH,
    SECURITY_QUESTIONS,
    decoy_question_key,
    normalize_answer,
)
from src.utils.auth import (
    create_password_reset_token,
    hash_password,
    verify_password,
    verify_password_reset_token,
)
from src.utils.phone import GHANA_PHONE_ERROR, mask_phone, normalize_ghana_phone

logger = logging.getLogger("admin_accounts.routes")

router = APIRouter(prefix="/auth", tags=["admin-accounts"])

SECURITY_ANSWER_MAX_ATTEMPTS = 3

RESET_REQUEST_MESSAGE = (
    "If an account with a verified phone matches that email, we've sent it a code. "
    f"It expires in {OTP_TTL_MINUTES} minutes."
)
RESET_CODE_FAILED = "That code is incorrect or has expired. Request a new code if you need one."
RESET_ANSWER_FAILED = (
    "That answer wasn't accepted. After several incorrect answers this option is locked — "
    "use the SMS code option instead."
)
RESET_GRANT_INVALID = "This reset session has expired or was already used. Please start again."

# A precomputed hash to verify against when no account matches, so unknown
# emails cost about the same time as real ones.
_DUMMY_HASH = hash_password("dummy-password-for-timing-equalisation")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _question_list() -> list[dict]:
    return [{"key": key, "text": text} for key, text in SECURITY_QUESTIONS.items()]


def _validate_new_password(admin: AdminUser, new_password: str, confirm_password: str) -> None:
    error = password_policy_error(new_password)
    if error:
        raise _bad_request(error)
    if new_password != confirm_password:
        raise _bad_request("The two passwords don't match.")
    if verify_password(new_password, admin.password_hash):
        raise _bad_request("Choose a password different from your current one.")


def _apply_new_password(admin: AdminUser, new_password: str) -> None:
    admin.password_hash = hash_password(new_password)
    # Invalidates every access/refresh token (and reset grant) issued so far.
    admin.token_version = int(admin.token_version or 0) + 1
    admin.security_answer_attempt_count = 0
    # Any successful password choice satisfies a platform-owner reset.
    admin.must_change_password = False


# ── Onboarding ────────────────────────────────────────────────────────────


class OnboardingPhoneRequest(BaseModel):
    phone: str


class OtpVerifyRequest(BaseModel):
    code: str = Field(min_length=1, max_length=12)


class OnboardingPasswordRequest(BaseModel):
    new_password: str = Field(max_length=256)
    confirm_password: str = Field(max_length=256)
    # Required during onboarding; optional after a platform-owner reset.
    security_question: str | None = None
    security_answer: str | None = Field(default=None, max_length=256)


def _setup_mode(admin: AdminUser) -> str | None:
    if admin.must_complete_onboarding:
        return "onboarding"
    if admin.must_change_password:
        return "change_password"
    return None


def _require_pending_setup(admin: AdminUser) -> str:
    mode = _setup_mode(admin)
    if mode is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Your account setup is already complete.")
    return mode


def _require_onboarding(admin: AdminUser) -> None:
    """Phone verification belongs to onboarding only; a password-change-only
    account already has a verified phone."""
    if _require_pending_setup(admin) != "onboarding":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Your phone is already verified. Choose a new password.")


@router.get("/onboarding/status")
async def onboarding_status(admin: AdminUser = Depends(get_authenticated_admin)):
    return {
        "mode": _setup_mode(admin),  # "onboarding" | "change_password" | None
        "must_complete_onboarding": bool(admin.must_complete_onboarding),
        "must_change_password": bool(admin.must_change_password),
        "has_security_question": bool(admin.security_question),
        "phone_verified": bool(admin.phone_verified),
        # Pre-fill hint only; the admin can verify a different number.
        "phone_on_file": mask_phone(admin.phone),
        "email": admin.email,
        "security_questions": _question_list(),
    }


@router.post("/onboarding/phone")
async def onboarding_send_code(
    body: OnboardingPhoneRequest,
    request: Request,
    admin: AdminUser = Depends(get_authenticated_admin),
    db: AsyncSession = Depends(get_db),
):
    _require_onboarding(admin)
    await enforce_rate_limit(_client_ip(request), "admin:onboarding-otp-send", limit=10, window_seconds=600)
    await enforce_rate_limit(f"acct:{admin.id}", "admin:onboarding-otp-send", limit=3, window_seconds=600)
    try:
        phone = normalize_ghana_phone(body.phone)
    except ValueError:
        raise _bad_request(GHANA_PHONE_ERROR)

    row, code = await otp_service.issue_code(db, admin_user_id=admin.id, purpose="onboarding", phone=phone)
    await db.commit()

    result = await send_otp_sms(phone, code, purpose="onboarding")
    if not result.success:
        # Never leave a live code the admin never received.
        row.consumed_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="We couldn't send the SMS right now. Check the number and try again in a moment.",
        )
    return {"status": "sent", "phone": mask_phone(phone), "expires_in_seconds": OTP_TTL_MINUTES * 60}


@router.post("/onboarding/otp/verify")
async def onboarding_verify_code(
    body: OtpVerifyRequest,
    request: Request,
    admin: AdminUser = Depends(get_authenticated_admin),
    db: AsyncSession = Depends(get_db),
):
    _require_onboarding(admin)
    await enforce_rate_limit(_client_ip(request), "admin:onboarding-otp-verify", limit=30, window_seconds=600)
    await enforce_rate_limit(f"acct:{admin.id}", "admin:onboarding-otp-verify", limit=10, window_seconds=600)

    result = await otp_service.verify_code(db, admin_user_id=admin.id, purpose="onboarding", code=body.code)
    if not result.ok:
        raise _bad_request(otp_service.failure_message(result))

    admin.phone = result.row.phone
    admin.phone_verified = True
    await db.commit()
    return {"status": "verified", "phone": mask_phone(admin.phone)}


@router.post("/onboarding/password")
async def onboarding_set_password(
    body: OnboardingPasswordRequest,
    request: Request,
    admin: AdminUser = Depends(get_authenticated_admin),
    db: AsyncSession = Depends(get_db),
):
    mode = _require_pending_setup(admin)
    await enforce_rate_limit(f"acct:{admin.id}", "admin:onboarding-password", limit=10, window_seconds=600)
    if mode == "onboarding" and not admin.phone_verified:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Verify your phone number first.")

    question_given = bool(body.security_question) or bool((body.security_answer or "").strip())
    if mode == "onboarding" or question_given:
        if body.security_question not in SECURITY_QUESTIONS:
            raise _bad_request("Choose one of the listed security questions.")
        answer = normalize_answer(body.security_answer or "")
        if len(answer) < ANSWER_MIN_LENGTH:
            raise _bad_request("Enter an answer to your security question.")
    _validate_new_password(admin, body.new_password, body.confirm_password)

    _apply_new_password(admin, body.new_password)
    if mode == "onboarding" or question_given:
        admin.security_question = body.security_question
        admin.security_answer_hash = hash_password(answer)
    admin.must_complete_onboarding = False
    await db.commit()
    logger.info("admin_setup_password_set admin_id=%s mode=%s", admin.id, mode)
    # The temp-password session is now invalid (token_version bumped).
    return {"status": "complete", "logout": True}


# ── Change your own password (signed in) ──────────────────────────────────


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)
    confirm_password: str = Field(max_length=256)


@router.post("/me/password", response_model=TokenResponse)
async def change_my_password(
    body: ChangePasswordRequest,
    request: Request,
    admin: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change your own password while signed in.

    Bumping token_version kills every other session (and any outstanding reset
    grant) immediately; the fresh token pair returned here keeps the caller
    signed in, so the person isn't logged out of the request they just made.
    Depends on get_current_user, so an account still in onboarding or with a
    pending platform reset uses the onboarding flow instead.
    """
    await enforce_rate_limit(_client_ip(request), "admin:password-change", limit=10, window_seconds=900)
    await enforce_rate_limit(f"acct:{admin.id}", "admin:password-change", limit=5, window_seconds=900)

    if not verify_password(body.current_password, admin.password_hash):
        raise _bad_request("Your current password is incorrect.")
    _validate_new_password(admin, body.new_password, body.confirm_password)

    _apply_new_password(admin, body.new_password)
    await db.execute(
        update(AdminOtpCode)
        .where(AdminOtpCode.admin_user_id == admin.id, AdminOtpCode.consumed_at.is_(None))
        .values(consumed_at=datetime.now(timezone.utc))
    )
    await db.commit()
    await db.refresh(admin)
    logger.info("admin_password_changed admin_id=%s", admin.id)
    return admin_token_response(admin)


# ── Forgot password ───────────────────────────────────────────────────────


class ResetRequest(BaseModel):
    email: str = Field(max_length=320)


class ResetVerifyOtpRequest(BaseModel):
    email: str = Field(max_length=320)
    code: str = Field(min_length=1, max_length=12)


class SecurityQuestionRequest(BaseModel):
    email: str = Field(max_length=320)
    # Omit to look up the question; supply to answer it.
    answer: str | None = Field(default=None, max_length=256)


class ResetSetPasswordRequest(BaseModel):
    reset_token: str = Field(max_length=2048)
    new_password: str = Field(max_length=256)
    confirm_password: str = Field(max_length=256)


def _email_key(email: str) -> str:
    return (email or "").strip().lower()


def _email_bucket(email: str) -> str:
    """Per-account rate-limit key without putting the address in Redis/logs."""
    return "acct:" + hashlib.sha256(_email_key(email).encode()).hexdigest()[:24]


async def _resettable_admin(db: AsyncSession, email: str, *, lock: bool = False) -> AdminUser | None:
    """An active admin who finished onboarding and has a verified phone. Accounts
    still on a temp password are not resettable — onboarding is their path."""
    stmt = select(AdminUser).where(
        func.lower(AdminUser.email) == _email_key(email),
        AdminUser.is_active == True,  # noqa: E712
        AdminUser.must_complete_onboarding == False,  # noqa: E712
        AdminUser.phone_verified == True,  # noqa: E712
        AdminUser.phone.is_not(None),
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def _send_reset_code_in_background(phone: str, code: str) -> None:
    result = await send_otp_sms(phone, code, purpose="reset")
    if not result.success:
        logger.error("admin_reset_sms_failed phone=%s error=%s", mask_phone(phone), result.error)


@router.post("/reset/request")
async def reset_request(
    body: ResetRequest,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    email = _email_key(body.email)
    await enforce_rate_limit(_client_ip(request), "admin:reset-request", limit=5, window_seconds=900)
    await enforce_rate_limit(_email_bucket(email), "admin:reset-request", limit=3, window_seconds=900)

    admin = await _resettable_admin(db, email)
    if admin is None:
        verify_password("000000", _DUMMY_HASH)  # comparable work to issuing a code
        return {"status": "ok", "message": RESET_REQUEST_MESSAGE}

    _, code = await otp_service.issue_code(db, admin_user_id=admin.id, purpose="reset", phone=admin.phone)
    await db.commit()
    # Sent after the response so SMS latency can't reveal that the account exists.
    background.add_task(_send_reset_code_in_background, admin.phone, code)
    return {"status": "ok", "message": RESET_REQUEST_MESSAGE}


@router.post("/reset/verify-otp")
async def reset_verify_otp(body: ResetVerifyOtpRequest, request: Request, db: AsyncSession = Depends(get_db)):
    email = _email_key(body.email)
    await enforce_rate_limit(_client_ip(request), "admin:reset-verify", limit=20, window_seconds=900)
    await enforce_rate_limit(_email_bucket(email), "admin:reset-verify", limit=10, window_seconds=900)

    admin = await _resettable_admin(db, email)
    if admin is None:
        verify_password("000000", _DUMMY_HASH)
        raise _bad_request(RESET_CODE_FAILED)

    result = await otp_service.verify_code(db, admin_user_id=admin.id, purpose="reset", code=body.code)
    if not result.ok:
        # One message for every failure mode: a distinct "locked"/"expired"
        # answer would tell an attacker the account is real.
        raise _bad_request(RESET_CODE_FAILED)
    await db.commit()
    return {
        "status": "verified",
        "reset_token": create_password_reset_token(
            admin_id=str(admin.id), token_version=int(admin.token_version or 0), via="otp"
        ),
    }


@router.post("/reset/security-question")
async def reset_security_question(body: SecurityQuestionRequest, request: Request, db: AsyncSession = Depends(get_db)):
    email = _email_key(body.email)
    await enforce_rate_limit(_client_ip(request), "admin:reset-question", limit=20, window_seconds=900)

    admin = await _resettable_admin(db, email, lock=body.answer is not None)
    real = admin is not None and bool(admin.security_question) and bool(admin.security_answer_hash)
    question_key = admin.security_question if real else decoy_question_key(email)

    if body.answer is None:
        return {"question_key": question_key, "question": SECURITY_QUESTIONS[question_key]}

    # Applies to real and unknown emails alike, so hitting it reveals nothing.
    await enforce_rate_limit(_email_bucket(email), "admin:reset-answer", limit=SECURITY_ANSWER_MAX_ATTEMPTS + 2, window_seconds=900)
    answer = normalize_answer(body.answer)

    if not real:
        verify_password(answer, _DUMMY_HASH)
        raise _bad_request(RESET_ANSWER_FAILED)

    if int(admin.security_answer_attempt_count or 0) >= SECURITY_ANSWER_MAX_ATTEMPTS:
        # Locked until a successful reset (via OTP) clears the counter.
        verify_password(answer, _DUMMY_HASH)
        raise _bad_request(RESET_ANSWER_FAILED)

    if not verify_password(answer, admin.security_answer_hash):
        admin.security_answer_attempt_count = int(admin.security_answer_attempt_count or 0) + 1
        await db.commit()
        if admin.security_answer_attempt_count >= SECURITY_ANSWER_MAX_ATTEMPTS:
            logger.warning("admin_security_answer_locked admin_id=%s", admin.id)
        raise _bad_request(RESET_ANSWER_FAILED)

    await db.commit()
    return {
        "status": "verified",
        "reset_token": create_password_reset_token(
            admin_id=str(admin.id), token_version=int(admin.token_version or 0), via="security_question"
        ),
    }


@router.post("/reset/set-password")
async def reset_set_password(body: ResetSetPasswordRequest, request: Request, db: AsyncSession = Depends(get_db)):
    await enforce_rate_limit(_client_ip(request), "admin:reset-set-password", limit=10, window_seconds=900)
    grant = verify_password_reset_token(body.reset_token)
    if grant is None:
        raise _bad_request(RESET_GRANT_INVALID)

    admin = (
        await db.execute(
            select(AdminUser)
            .where(
                AdminUser.id == grant["sub"],
                AdminUser.is_active == True,  # noqa: E712
                AdminUser.must_complete_onboarding == False,  # noqa: E712
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    # token_version moves on every password change, so a grant is single-use.
    if admin is None or grant.get("tv") != int(admin.token_version or 0):
        raise _bad_request(RESET_GRANT_INVALID)

    _validate_new_password(admin, body.new_password, body.confirm_password)
    _apply_new_password(admin, body.new_password)
    await db.execute(
        update(AdminOtpCode)
        .where(
            AdminOtpCode.admin_user_id == admin.id,
            AdminOtpCode.purpose == "reset",
            AdminOtpCode.consumed_at.is_(None),
        )
        .values(consumed_at=datetime.now(timezone.utc))
    )
    await db.commit()
    logger.info("admin_password_reset admin_id=%s via=%s", admin.id, grant.get("via"))
    return {"status": "password_reset"}
