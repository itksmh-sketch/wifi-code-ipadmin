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
    GET  /auth/me/security              everything the Security page renders
    POST /auth/me/password              change your own password
    POST /auth/me/pin                   set or replace the PIN
    POST /auth/me/pin/verify            open the gated areas for the window
    POST /auth/me/pin/forgot            SMS a code to the verified phone
    POST /auth/me/pin/reset             code + password -> new PIN, lock cleared
    POST /auth/me/phone                 SMS a code to a proposed new number
    POST /auth/me/phone/verify          code -> number replaced, old one warned
    POST /auth/me/security-question     replace the question and answer

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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import async_session_factory, get_db
from src.db.models import AdminOtpCode, AdminSecurityEvent, AdminUser
from src.schemas import TokenResponse
from src.middleware.auth import (
    PIN_REQUIRED_HEADER,
    get_authenticated_admin,
    get_current_user,
    pin_elevation_error,
    require_recent_pin,
)
from src.middleware.rate_limit import enforce_rate_limit
from src.modules.admin_accounts import lockout, otp as otp_service
from src.modules.admin_accounts.notifications import OTP_TTL_MINUTES, send_otp_sms
from src.modules.admin_accounts.passwords import password_policy_error
from src.modules.admin_accounts.pins import PIN_ELEVATION_MINUTES, pin_policy_error
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
    # Setting a password proves ownership, so it also ends both lockouts —
    # holding the rightful owner out for the rest of a 3h window is pure
    # downside, and this is the documented way to un-stick a locked account.
    lockout.clear_all(admin)
    # A new password does not re-open the gated areas: elevation is a separate
    # proof and has to be re-earned by entering the PIN.
    admin.pin_verified_until = None
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


# ── PIN (signed in) ───────────────────────────────────────────────────────
#
# A second factor in front of the Security and Payments areas. It is not a
# second password: the password proves who you are at sign-in, the PIN proves
# someone is still at the keyboard before a change that moves money or moves
# the account itself. Hence a short elevation window rather than a session-long
# one, and a much higher failure budget than login gets — a mistyped PIN on
# your own laptop is ordinary, whereas five failed passwords is not.

PIN_LOCKED_MESSAGE = "Too many incorrect PIN entries. Try again later, or reset your PIN."
PIN_NOT_SET_MESSAGE = "You haven't set a PIN yet."


class SetPinRequest(BaseModel):
    current_password: str = Field(max_length=256)
    new_pin: str = Field(max_length=32)
    confirm_pin: str = Field(max_length=32)
    # Required only when replacing an existing PIN.
    current_pin: str | None = Field(default=None, max_length=32)


class VerifyPinRequest(BaseModel):
    pin: str = Field(max_length=32)


class ResetPinRequest(BaseModel):
    code: str = Field(min_length=1, max_length=12)
    current_password: str = Field(max_length=256)
    new_pin: str = Field(max_length=32)
    confirm_pin: str = Field(max_length=32)


def _record_event(db: AsyncSession, admin: AdminUser, event_type: str, **detail) -> AdminSecurityEvent:
    """Add an audit row. Caller commits. Detail must stay free of secrets."""
    kept = {k: v for k, v in detail.items() if v is not None}
    row = AdminSecurityEvent(
        admin_user_id=admin.id,
        isp_operator_id=admin.isp_operator_id,
        event_type=event_type,
        detail=kept or None,
    )
    db.add(row)
    return row


def _elevate(admin: AdminUser) -> datetime:
    """Open the gated areas for the fixed elevation window."""
    until = datetime.now(timezone.utc) + timedelta(minutes=PIN_ELEVATION_MINUTES)
    admin.pin_verified_until = until
    return until


def _pin_locked_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=PIN_LOCKED_MESSAGE,
        headers={PIN_REQUIRED_HEADER: "locked"},
    )


def _pin_locked_response(background_tasks: BackgroundTasks) -> JSONResponse:
    """The same 403 as _pin_locked_error, but RETURNED so its background task
    survives. FastAPI attaches background tasks to the response a handler
    returns; when a handler raises, the exception handler builds a fresh
    response and the tasks are dropped on the floor — which silently disabled
    the lockout SMS. Byte-identical to the raised version from outside."""
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"detail": PIN_LOCKED_MESSAGE},
        headers={PIN_REQUIRED_HEADER: "locked"},
        background=background_tasks,
    )


async def _count_pin_failure(
    db: AsyncSession,
    admin: AdminUser,
    background_tasks: BackgroundTasks,
    client_ip: str,
) -> bool:
    """Record a wrong PIN. True if that tripped the lock (sessions now dead)."""
    event_id = await lockout.register_failure(db, admin, lockout.PIN, client_ip=client_ip)
    if event_id is None:
        return False
    background_tasks.add_task(lockout.send_lockout_notification, admin.id, lockout.PIN, event_id)
    return True


def _attempts_remaining(admin: AdminUser) -> int:
    return max(0, lockout.PIN_MAX_ATTEMPTS - int(admin.pin_attempt_count or 0))


@router.post("/me/pin")
async def set_my_pin(
    body: SetPinRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    admin: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set a first PIN, or replace an existing one.

    Always requires the current password. Requiring it even for a first PIN is
    the point: otherwise anyone who walks up to an unlocked browser could set a
    PIN of their own and lock the real owner out of their own Payments page.
    """
    client_ip = _client_ip(request)
    await enforce_rate_limit(client_ip, "admin:pin-set", limit=10, window_seconds=900)
    await enforce_rate_limit(f"acct:{admin.id}", "admin:pin-set", limit=5, window_seconds=900)

    if not verify_password(body.current_password, admin.password_hash):
        raise _bad_request("Your current password is incorrect.")

    replacing = bool(admin.pin_hash)
    if replacing:
        # Gated only on this branch. A first-time setup cannot require
        # elevation — there is no PIN to earn it with — so the guard cannot be
        # a route dependency; it has to run here, where "setup or change" is
        # finally known. Recovery when the PIN is forgotten goes through
        # /pin/forgot + /pin/reset, which are deliberately ungated.
        elevation_error = pin_elevation_error(admin)
        if elevation_error is not None:
            raise elevation_error
        if lockout.is_locked(admin, lockout.PIN):
            raise _pin_locked_error()
        if not body.current_pin or not verify_password(body.current_pin, admin.pin_hash):
            if await _count_pin_failure(db, admin, background_tasks, client_ip):
                return _pin_locked_response(background_tasks)
            raise _bad_request(f"Your current PIN is incorrect. {_attempts_remaining(admin)} attempts remaining.")

    if body.new_pin != body.confirm_pin:
        raise _bad_request("The two PINs don't match.")
    error = pin_policy_error(body.new_pin)
    if error:
        raise _bad_request(error)
    if replacing and verify_password(body.new_pin, admin.pin_hash):
        raise _bad_request("Choose a PIN different from your current one.")

    admin.pin_hash = hash_password(body.new_pin)
    admin.pin_set_at = datetime.now(timezone.utc)
    lockout.clear(admin, lockout.PIN)
    until = _elevate(admin)
    _record_event(db, admin, "pin_changed" if replacing else "pin_set", client_ip=client_ip)
    await db.commit()
    logger.info("admin_pin_%s admin_id=%s", "changed" if replacing else "set", admin.id)
    return {"status": "saved", "verified_until": until.isoformat()}


@router.post("/me/pin/verify")
async def verify_my_pin(
    body: VerifyPinRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    admin: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Enter the PIN to open the gated areas for the elevation window."""
    client_ip = _client_ip(request)
    await enforce_rate_limit(client_ip, "admin:pin-verify", limit=60, window_seconds=900)
    # The DB counter is what actually stops guessing (the Redis limiter fails
    # open); this is only here to keep the bcrypt work off a hot loop.
    await enforce_rate_limit(f"acct:{admin.id}", "admin:pin-verify", limit=30, window_seconds=900)

    if not admin.pin_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=PIN_NOT_SET_MESSAGE,
            headers={PIN_REQUIRED_HEADER: "setup"},
        )
    if lockout.is_locked(admin, lockout.PIN):
        raise _pin_locked_error()

    if not verify_password(body.pin, admin.pin_hash):
        if await _count_pin_failure(db, admin, background_tasks, client_ip):
            # token_version moved, so this session is already dead; the next
            # request gets a 401 and the UI bounces to login.
            return _pin_locked_response(background_tasks)
        raise _bad_request(f"Incorrect PIN. {_attempts_remaining(admin)} attempts remaining.")

    lockout.clear(admin, lockout.PIN)
    until = _elevate(admin)
    await db.commit()
    return {"status": "verified", "verified_until": until.isoformat()}


@router.post("/me/pin/forgot")
async def forgot_my_pin(
    request: Request,
    admin: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """SMS a code to the verified phone so a forgotten PIN can be replaced.

    Without this, one forgotten PIN would brick the Payments area until a
    platform owner intervened. Deliberately reachable while the PIN is locked —
    it is the way out of a lockout, and it proves possession of the phone
    rather than knowledge of the thing that is locked.
    """
    await enforce_rate_limit(_client_ip(request), "admin:pin-forgot", limit=10, window_seconds=900)
    await enforce_rate_limit(f"acct:{admin.id}", "admin:pin-forgot", limit=3, window_seconds=900)

    if not (admin.phone_verified and admin.phone):
        raise _bad_request(
            "Your phone isn't verified, so we can't send a reset code. Ask platform support to reset your account."
        )

    row, code = await otp_service.issue_code(db, admin_user_id=admin.id, purpose="pin_reset", phone=admin.phone)
    await db.commit()

    result = await send_otp_sms(admin.phone, code, purpose="pin_reset")
    if not result.success:
        # Never leave a live code the admin never received.
        row.consumed_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="We couldn't send the SMS right now. Please try again in a moment.",
        )
    return {"status": "sent", "phone": mask_phone(admin.phone), "expires_in_seconds": OTP_TTL_MINUTES * 60}


@router.post("/me/pin/reset")
async def reset_my_pin(
    body: ResetPinRequest,
    request: Request,
    admin: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the PIN using the SMS code plus the current password.

    Two factors, neither of which is the PIN, so this works while locked out —
    and clears the lock, since holding possession of both the password and the
    phone is a stronger claim than the PIN it replaces.
    """
    await enforce_rate_limit(_client_ip(request), "admin:pin-reset", limit=10, window_seconds=900)
    await enforce_rate_limit(f"acct:{admin.id}", "admin:pin-reset", limit=10, window_seconds=900)

    if not verify_password(body.current_password, admin.password_hash):
        raise _bad_request("Your current password is incorrect.")

    result = await otp_service.verify_code(db, admin_user_id=admin.id, purpose="pin_reset", code=body.code)
    if not result.ok:
        raise _bad_request(otp_service.failure_message(result))

    if body.new_pin != body.confirm_pin:
        raise _bad_request("The two PINs don't match.")
    error = pin_policy_error(body.new_pin)
    if error:
        raise _bad_request(error)

    admin.pin_hash = hash_password(body.new_pin)
    admin.pin_set_at = datetime.now(timezone.utc)
    lockout.clear(admin, lockout.PIN)
    until = _elevate(admin)
    _record_event(db, admin, "pin_changed", client_ip=_client_ip(request), via="otp_reset")
    await db.commit()
    logger.info("admin_pin_reset admin_id=%s", admin.id)
    return {"status": "saved", "verified_until": until.isoformat()}


# ── Phone re-verification and security question (signed in) ───────────────
#
# The verified phone is the recovery channel for forgot-password AND forgot-PIN,
# so it is the highest-value field on the account: whoever controls it can
# eventually control everything else. Three things follow, and all three are
# load-bearing rather than decorative:
#   * the new number must prove itself by OTP before it replaces the old one,
#     so a typo cannot strand the account on a number nobody answers;
#   * the number being replaced is told, on its way out, that it was replaced;
#   * changes are capped at three per rolling thirty days, so an attacker who
#     reaches a live session cannot simply cycle numbers until one sticks.

PHONE_CHANGE_LIMIT = 3
PHONE_CHANGE_WINDOW_DAYS = 30


class PhoneChangeRequest(BaseModel):
    phone: str = Field(max_length=32)


class ChangeSecurityQuestionRequest(BaseModel):
    current_password: str = Field(max_length=256)
    security_question: str
    security_answer: str = Field(max_length=256)


async def _phone_change_quota(db: AsyncSession, admin: AdminUser) -> tuple[int, datetime | None]:
    """(changes inside the window, when the window next frees a slot).

    Counts only "phone_changed" rows — a re-verification of the number already
    on file writes "phone_reverified" instead and is deliberately free, since
    confirming you still hold your own number is not a change and shouldn't be
    rationed. Reading the rows rather than COUNT(*) is what makes the second
    return value possible: the window is rolling, so the next slot opens
    thirty days after the OLDEST change still inside it, not at a fixed reset.
    """
    since = datetime.now(timezone.utc) - timedelta(days=PHONE_CHANGE_WINDOW_DAYS)
    rows = (
        await db.execute(
            select(AdminSecurityEvent.created_at)
            .where(
                AdminSecurityEvent.admin_user_id == admin.id,
                AdminSecurityEvent.event_type == "phone_changed",
                AdminSecurityEvent.created_at > since,
            )
            .order_by(AdminSecurityEvent.created_at.asc())
        )
    ).scalars().all()
    frees_at = rows[0] + timedelta(days=PHONE_CHANGE_WINDOW_DAYS) if rows else None
    return len(rows), frees_at


def _quota_exhausted_error(frees_at: datetime | None) -> HTTPException:
    when = f" You can change it again after {frees_at:%d %b %Y}." if frees_at else ""
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            f"You've changed your phone number {PHONE_CHANGE_LIMIT} times in the last "
            f"{PHONE_CHANGE_WINDOW_DAYS} days.{when}"
        ),
    )


async def _notify_old_number(old_phone: str, admin_id) -> None:
    """Background task: warn the number that just lost the account."""
    from src.modules.admin_accounts.notifications import send_phone_changed_sms

    try:
        async with async_session_factory() as db:
            admin = (await db.execute(select(AdminUser).where(AdminUser.id == admin_id))).scalar_one_or_none()
            if admin is not None:
                await send_phone_changed_sms(old_phone, admin)
    except Exception as exc:  # never surfaces as an error on the request
        logger.error("admin_phone_change_notice_failed admin_id=%s error=%s", admin_id, exc)


@router.get("/me/security")
async def my_security_status(
    admin: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Everything the Security page needs to render itself in one call."""
    used, frees_at = await _phone_change_quota(db, admin)
    pin_locked = lockout.locked_until(admin, lockout.PIN)
    now = datetime.now(timezone.utc)
    elevated = admin.pin_verified_until is not None and admin.pin_verified_until > now
    return {
        "email": admin.email,
        "phone": mask_phone(admin.phone),
        "phone_verified": bool(admin.phone_verified),
        "phone_changes_used": used,
        "phone_changes_limit": PHONE_CHANGE_LIMIT,
        "phone_change_window_days": PHONE_CHANGE_WINDOW_DAYS,
        "phone_change_window_resets_at": frees_at.isoformat() if frees_at else None,
        "security_question": admin.security_question,
        "security_questions": _question_list(),
        "has_pin": bool(admin.pin_hash),
        "pin_set_at": admin.pin_set_at.isoformat() if admin.pin_set_at else None,
        "pin_locked_until": pin_locked.isoformat() if pin_locked else None,
        "pin_attempts_remaining": _attempts_remaining(admin),
        "pin_verified_until": admin.pin_verified_until.isoformat() if elevated else None,
    }


@router.post("/me/phone", dependencies=[Depends(require_recent_pin)])
async def request_phone_change(
    body: PhoneChangeRequest,
    request: Request,
    admin: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a code to a number to prove it before it becomes the number on file.

    The code goes to the PROPOSED number, not the current one: the thing being
    established is that this new handset exists and is reachable. The current
    number's say in the matter is the notice it receives afterwards.
    """
    await enforce_rate_limit(_client_ip(request), "admin:phone-change-send", limit=10, window_seconds=900)
    await enforce_rate_limit(f"acct:{admin.id}", "admin:phone-change-send", limit=5, window_seconds=900)

    try:
        phone = normalize_ghana_phone(body.phone)
    except ValueError:
        raise _bad_request(GHANA_PHONE_ERROR)

    changing = phone != (admin.phone or "")
    if changing:
        # Checked here as well as at verify so an exhausted quota costs neither
        # an SMS nor the admin's time typing in a code that cannot be redeemed.
        used, frees_at = await _phone_change_quota(db, admin)
        if used >= PHONE_CHANGE_LIMIT:
            raise _quota_exhausted_error(frees_at)

    row, code = await otp_service.issue_code(db, admin_user_id=admin.id, purpose="phone_change", phone=phone)
    await db.commit()

    result = await send_otp_sms(phone, code, purpose="phone_change")
    if not result.success:
        row.consumed_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="We couldn't send the SMS right now. Check the number and try again in a moment.",
        )
    return {
        "status": "sent",
        "phone": mask_phone(phone),
        "changing": changing,
        "expires_in_seconds": OTP_TTL_MINUTES * 60,
    }


@router.post("/me/phone/verify", dependencies=[Depends(require_recent_pin)])
async def verify_phone_change(
    body: OtpVerifyRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    admin: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await enforce_rate_limit(_client_ip(request), "admin:phone-change-verify", limit=30, window_seconds=900)
    await enforce_rate_limit(f"acct:{admin.id}", "admin:phone-change-verify", limit=10, window_seconds=900)

    result = await otp_service.verify_code(db, admin_user_id=admin.id, purpose="phone_change", code=body.code)
    if not result.ok:
        raise _bad_request(otp_service.failure_message(result))

    new_phone = result.row.phone
    old_phone = admin.phone or ""
    changed = new_phone != old_phone

    if changed:
        # Re-checked against the live window: minutes passed while the code was
        # in flight, and a code issued under quota must not be redeemable once
        # a concurrent change has used the last slot.
        used, frees_at = await _phone_change_quota(db, admin)
        if used >= PHONE_CHANGE_LIMIT:
            # Commit so the code stays consumed — verify_code only flushed it,
            # and a rollback here would hand back a replayable code.
            await db.commit()
            raise _quota_exhausted_error(frees_at)

    admin.phone = new_phone
    admin.phone_verified = True
    if changed:
        _record_event(
            db,
            admin,
            "phone_changed",
            client_ip=_client_ip(request),
            old_phone=mask_phone(old_phone) or None,
            new_phone=mask_phone(new_phone),
        )
    else:
        # Free: re-confirming the number already on file is not a change.
        _record_event(db, admin, "phone_reverified", client_ip=_client_ip(request))
    await db.commit()
    logger.info("admin_phone_%s admin_id=%s", "changed" if changed else "reverified", admin.id)

    if changed and old_phone:
        background_tasks.add_task(_notify_old_number, old_phone, admin.id)

    used, _ = await _phone_change_quota(db, admin)
    return {
        "status": "verified",
        "phone": mask_phone(new_phone),
        "changed": changed,
        "phone_changes_used": used,
        "phone_changes_limit": PHONE_CHANGE_LIMIT,
    }


@router.post("/me/security-question", dependencies=[Depends(require_recent_pin)])
async def change_my_security_question(
    body: ChangeSecurityQuestionRequest,
    request: Request,
    admin: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the security question and answer.

    Gated on the current password rather than on the old answer: the answer is
    a recovery factor, and requiring it here would mean an admin who has
    forgotten it can never replace it — the exact situation in which replacing
    it is what they need to do.
    """
    await enforce_rate_limit(_client_ip(request), "admin:security-question", limit=10, window_seconds=900)
    await enforce_rate_limit(f"acct:{admin.id}", "admin:security-question", limit=5, window_seconds=900)

    if not verify_password(body.current_password, admin.password_hash):
        raise _bad_request("Your current password is incorrect.")
    if body.security_question not in SECURITY_QUESTIONS:
        raise _bad_request("Choose one of the listed security questions.")
    answer = normalize_answer(body.security_answer)
    if len(answer) < ANSWER_MIN_LENGTH:
        raise _bad_request("Enter an answer to your security question.")

    admin.security_question = body.security_question
    admin.security_answer_hash = hash_password(answer)
    # A brand-new answer starts with a clean slate of attempts; the strikes
    # belonged to the answer being replaced.
    admin.security_answer_attempt_count = 0
    _record_event(db, admin, "security_question_changed", client_ip=_client_ip(request))
    await db.commit()
    logger.info("admin_security_question_changed admin_id=%s", admin.id)
    return {"status": "saved", "security_question": admin.security_question}
