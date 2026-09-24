"""Platform-owner security setup: verify a phone, set a security question.

These are the first two steps of the owner's security setup (the character
code and the all-or-nothing completion come later). They only accept calls
while ``must_complete_security_setup`` is TRUE. Once setup is complete, the
phone is the recovery channel for both forgot-password and forgot-code, and
re-pointing it from a signed-in session is exactly the takeover path that
send_phone_changed_sms exists to catch on the admin side. Changing either
after setup needs its own flow with stronger proof, not these endpoints.

Every step that binds a recovery factor requires the current password, even
though the caller is signed in. The session alone would let anyone holding a
stolen or unattended owner token point recovery at their own phone and
question before the real owner finishes setup. Same reasoning as the admin
PIN setup (admin_accounts/routes.py set_my_pin).

The steps are resumable and idempotent: each can be repeated until setup
completes, and the latest verified phone / saved question wins.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import PlatformOwner, PlatformOwnerSecurityEvent
from src.middleware.auth import get_platform_owner_context
from src.middleware.rate_limit import enforce_rate_limit
from src.modules.admin_accounts import otp as otp_service
from src.modules.admin_accounts.notifications import OTP_TTL_MINUTES, send_otp_sms
from src.modules.admin_accounts.security_questions import ANSWER_MIN_LENGTH, SECURITY_QUESTIONS, normalize_answer
from src.utils.auth import hash_password, verify_password
from src.utils.phone import GHANA_PHONE_ERROR, mask_phone, normalize_ghana_phone

router = APIRouter(prefix="/platform/setup", tags=["platform"])
logger = logging.getLogger("platform.owner_security")

WRONG_PASSWORD = "Your current password is incorrect."


class SetupPhoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(max_length=64)
    current_password: str = Field(max_length=256)


class SetupOtpVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=12)


class SetupSecurityQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    security_question: str = Field(max_length=64)
    security_answer: str = Field(max_length=256)
    current_password: str = Field(max_length=256)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _require_setup_pending(owner: PlatformOwner) -> None:
    if not owner.must_complete_security_setup:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Your security setup is already complete.")


def _record_event(db: AsyncSession, owner: PlatformOwner, event_type: str, **detail) -> None:
    db.add(PlatformOwnerSecurityEvent(platform_owner_id=owner.id, event_type=event_type, detail=detail or None))


@router.get("/status")
async def setup_status(owner: PlatformOwner = Depends(get_platform_owner_context)):
    return {
        "must_complete_security_setup": bool(owner.must_complete_security_setup),
        "phone_verified": bool(owner.phone_verified),
        "phone": mask_phone(owner.phone) if owner.phone else None,
        "has_security_question": bool(owner.security_question and owner.security_answer_hash),
        "security_question": owner.security_question,
        "has_character_code": owner.challenge_hashes is not None,
        "security_questions": [{"key": key, "question": text} for key, text in SECURITY_QUESTIONS.items()],
    }


@router.post("/phone")
async def setup_send_phone_code(
    body: SetupPhoneRequest,
    request: Request,
    owner: PlatformOwner = Depends(get_platform_owner_context),
    db: AsyncSession = Depends(get_db),
):
    _require_setup_pending(owner)
    await enforce_rate_limit(_client_ip(request), "platform:setup-otp-send", limit=10, window_seconds=600)
    await enforce_rate_limit(f"owner:{owner.id}", "platform:setup-otp-send", limit=3, window_seconds=600)
    if not verify_password(body.current_password, owner.password_hash):
        raise _bad_request(WRONG_PASSWORD)
    try:
        phone = normalize_ghana_phone(body.phone)
    except ValueError:
        raise _bad_request(GHANA_PHONE_ERROR)

    row, code = await otp_service.issue_code(db, platform_owner_id=owner.id, purpose="setup", phone=phone)
    await db.commit()

    result = await send_otp_sms(phone, code, purpose="setup")
    if not result.success:
        # Never leave a live code the owner never received.
        row.consumed_at = datetime.now(timezone.utc)
        await db.commit()
        logger.error("platform_owner_setup_sms_failed owner_id=%s phone=%s error=%s", owner.id, mask_phone(phone), result.error)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="We couldn't send the SMS right now. Check the number and the platform SMS settings, then try again.",
        )
    return {"status": "sent", "phone": mask_phone(phone), "expires_in_seconds": OTP_TTL_MINUTES * 60}


@router.post("/phone/verify")
async def setup_verify_phone_code(
    body: SetupOtpVerifyRequest,
    request: Request,
    owner: PlatformOwner = Depends(get_platform_owner_context),
    db: AsyncSession = Depends(get_db),
):
    _require_setup_pending(owner)
    await enforce_rate_limit(_client_ip(request), "platform:setup-otp-verify", limit=30, window_seconds=600)
    await enforce_rate_limit(f"owner:{owner.id}", "platform:setup-otp-verify", limit=10, window_seconds=600)

    result = await otp_service.verify_code(db, platform_owner_id=owner.id, purpose="setup", code=body.code)
    if not result.ok:
        raise _bad_request(otp_service.failure_message(result))

    previous = owner.phone if owner.phone_verified else None
    owner.phone = result.row.phone
    owner.phone_verified = True
    _record_event(
        db, owner, "phone_verified",
        phone=mask_phone(owner.phone), replaced=mask_phone(previous) if previous and previous != owner.phone else None,
        client_ip=_client_ip(request),
    )
    await db.commit()
    logger.info("platform_owner_phone_verified owner_id=%s phone=%s", owner.id, mask_phone(owner.phone))
    return {"status": "verified", "phone": mask_phone(owner.phone)}


@router.post("/security-question")
async def setup_security_question(
    body: SetupSecurityQuestionRequest,
    request: Request,
    owner: PlatformOwner = Depends(get_platform_owner_context),
    db: AsyncSession = Depends(get_db),
):
    _require_setup_pending(owner)
    await enforce_rate_limit(f"owner:{owner.id}", "platform:setup-question", limit=5, window_seconds=900)
    if not verify_password(body.current_password, owner.password_hash):
        raise _bad_request(WRONG_PASSWORD)
    if body.security_question not in SECURITY_QUESTIONS:
        raise _bad_request("Choose one of the listed security questions.")
    answer = normalize_answer(body.security_answer)
    if len(answer) < ANSWER_MIN_LENGTH:
        raise _bad_request("Enter an answer to your security question.")

    replacing = bool(owner.security_answer_hash)
    owner.security_question = body.security_question
    owner.security_answer_hash = hash_password(answer)
    owner.security_answer_attempt_count = 0
    _record_event(db, owner, "security_question_set", question=body.security_question, replaced=replacing,
                  client_ip=_client_ip(request))
    await db.commit()
    logger.info("platform_owner_security_question_set owner_id=%s replaced=%s", owner.id, replacing)
    return {"status": "saved", "security_question": owner.security_question}
