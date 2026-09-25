"""Changing the platform owner's phone and security question after setup.

The setup endpoints (/platform/setup/*) refuse once setup is complete, so
without these the only way to change either factor was a break-glass
reset-setup and the whole wizard again.

These depend on the gated get_platform_owner_context, which already refuses
an owner whose setup is pending — the exact inverse of the setup routes — so
"only after setup" needs no check of its own, and the setup-gate route walk
(tests/test_platform_owner_setup_gate.py) covers them automatically.

Both factors are recovery channels, so a change is guarded three ways:
  * the current password, even though the caller is signed in (a stolen or
    unattended session alone must not be able to re-point recovery);
  * a new phone must prove itself with a code sent to it (OTP purpose
    "phone_change");
  * the owner is told on a channel a session hijacker can't read: a phone
    change texts the OLD number, a question change texts the verified phone.
    Sent after the change commits, best effort, outcome recorded on the
    security event (a failed text never undoes a change the owner proved).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import async_session_factory, get_db
from src.db.models import PlatformOwner, PlatformOwnerSecurityEvent
from src.middleware.auth import get_platform_owner_context
from src.middleware.rate_limit import enforce_rate_limit
from src.modules.admin_accounts import otp as otp_service
from src.modules.admin_accounts.notifications import (
    OTP_TTL_MINUTES,
    send_otp_sms,
    send_platform_owner_phone_changed_sms,
    send_platform_owner_question_changed_sms,
)
from src.modules.admin_accounts.security_questions import ANSWER_MIN_LENGTH, SECURITY_QUESTIONS, normalize_answer
from src.utils.auth import hash_password, verify_password
from src.utils.phone import GHANA_PHONE_ERROR, mask_phone, normalize_ghana_phone

router = APIRouter(prefix="/platform/me", tags=["platform"])
logger = logging.getLogger("platform.owner_account")

WRONG_PASSWORD = "Your current password is incorrect."


class PhoneChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(max_length=64)
    current_password: str = Field(max_length=256)


class PhoneChangeVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=12)


class SecurityQuestionChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    security_question: str = Field(max_length=64)
    security_answer: str = Field(max_length=256)
    current_password: str = Field(max_length=256)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


async def _alert(event_id: uuid.UUID, send) -> None:
    """Background task: send one security alert and record the outcome on its
    event row. Never raises — the change it reports has already committed."""
    try:
        result = await send()
        success, error = bool(result.success), (None if result.success else (result.error or "unknown"))
    except Exception as exc:  # a notification must never surface as a request error
        success, error = False, f"exception: {exc}"
    try:
        async with async_session_factory() as db:
            event = await db.get(PlatformOwnerSecurityEvent, event_id)
            if event is not None:
                event.sms_sent = success
                event.sms_error = error
                await db.commit()
    except Exception as exc:
        logger.error("platform_owner_alert_record_failed event_id=%s error=%s", event_id, exc)
    if not success:
        logger.error("platform_owner_alert_failed event_id=%s error=%s", event_id, error)


@router.post("/phone")
async def request_phone_change(
    body: PhoneChangeRequest,
    request: Request,
    owner: PlatformOwner = Depends(get_platform_owner_context),
    db: AsyncSession = Depends(get_db),
):
    """Step 1: send a code to the NEW number. Nothing about the account
    changes until that code comes back."""
    await enforce_rate_limit(_client_ip(request), "platform:phone-change", limit=10, window_seconds=600)
    await enforce_rate_limit(f"owner:{owner.id}", "platform:phone-change", limit=3, window_seconds=600)
    if not verify_password(body.current_password, owner.password_hash):
        raise _bad_request(WRONG_PASSWORD)
    try:
        phone = normalize_ghana_phone(body.phone)
    except ValueError:
        raise _bad_request(GHANA_PHONE_ERROR)
    if owner.phone_verified and phone == owner.phone:
        raise _bad_request("That is already your verified number.")

    row, code = await otp_service.issue_code(db, platform_owner_id=owner.id, purpose="phone_change", phone=phone)
    await db.commit()
    result = await send_otp_sms(phone, code, purpose="phone_change")
    if not result.success:
        row.consumed_at = datetime.now(timezone.utc)  # never leave a live code nobody received
        await db.commit()
        logger.error("platform_owner_phone_change_sms_failed owner_id=%s phone=%s error=%s",
                     owner.id, mask_phone(phone), result.error)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="We couldn't send the SMS right now. Check the number and try again in a moment.",
        )
    return {"status": "sent", "phone": mask_phone(phone), "expires_in_seconds": OTP_TTL_MINUTES * 60}


@router.post("/phone/verify")
async def verify_phone_change(
    body: PhoneChangeVerifyRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    owner: PlatformOwner = Depends(get_platform_owner_context),
    db: AsyncSession = Depends(get_db),
):
    """Step 2: the code from the new number makes it the verified phone, and
    the old number is told."""
    await enforce_rate_limit(_client_ip(request), "platform:phone-change-verify", limit=30, window_seconds=600)
    await enforce_rate_limit(f"owner:{owner.id}", "platform:phone-change-verify", limit=10, window_seconds=600)
    result = await otp_service.verify_code(db, platform_owner_id=owner.id, purpose="phone_change", code=body.code)
    if not result.ok:
        raise _bad_request(otp_service.failure_message(result))

    old_phone = owner.phone if owner.phone_verified else None
    owner.phone = result.row.phone
    owner.phone_verified = True
    event = PlatformOwnerSecurityEvent(
        platform_owner_id=owner.id, event_type="phone_changed",
        detail={"old_phone": mask_phone(old_phone) if old_phone else None,
                "new_phone": mask_phone(owner.phone), "client_ip": _client_ip(request)},
    )
    db.add(event)
    await db.commit()
    logger.warning("platform_owner_phone_changed owner_id=%s new=%s", owner.id, mask_phone(owner.phone))
    if old_phone and old_phone != owner.phone:
        new_masked = mask_phone(owner.phone)
        background_tasks.add_task(_alert, event.id, lambda: send_platform_owner_phone_changed_sms(old_phone, new_masked))
    return {"status": "changed", "phone": mask_phone(owner.phone)}


@router.post("/security-question")
async def change_security_question(
    body: SecurityQuestionChangeRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    owner: PlatformOwner = Depends(get_platform_owner_context),
    db: AsyncSession = Depends(get_db),
):
    await enforce_rate_limit(f"owner:{owner.id}", "platform:question-change", limit=5, window_seconds=900)
    if not verify_password(body.current_password, owner.password_hash):
        raise _bad_request(WRONG_PASSWORD)
    if body.security_question not in SECURITY_QUESTIONS:
        raise _bad_request("Choose one of the listed security questions.")
    answer = normalize_answer(body.security_answer)
    if len(answer) < ANSWER_MIN_LENGTH:
        raise _bad_request("Enter an answer to your security question.")

    owner.security_question = body.security_question
    owner.security_answer_hash = hash_password(answer)
    owner.security_answer_attempt_count = 0
    event = PlatformOwnerSecurityEvent(
        platform_owner_id=owner.id, event_type="security_question_changed",
        detail={"question": body.security_question, "client_ip": _client_ip(request)},
    )
    db.add(event)
    await db.commit()
    logger.warning("platform_owner_security_question_changed owner_id=%s", owner.id)
    if owner.phone_verified and owner.phone:
        phone = owner.phone
        background_tasks.add_task(_alert, event.id, lambda: send_platform_owner_question_changed_sms(phone))
    return {"status": "changed", "security_question": owner.security_question}
