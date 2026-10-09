"""Audit rows about the platform owner's own account: sign-in, sign-out,
profile, password, phone, security question and the security setup steps.

Platform-wide rows (no operator), aimed at the owner. Same three shapes as
the operator account rows in admin_accounts/routes.py:

  * ``own_row``: in the caller's transaction, by the owner;
  * ``own_row_detached``: the same, committed on its own, for an outcome
    that stands whatever the request does next (an SMS already sent, a
    refused attempt);
  * ``anonymous_row``: an attempt nobody has proved they own (a failed
    sign-in), by the system, written after the response has gone so that a
    real account and an unknown email take the same time to answer.

Never a password, answer, character code, OTP or token; a phone only as
mask_phone makes it.
"""
from __future__ import annotations

from fastapi import BackgroundTasks

from src.db.models import PlatformOwner
from src.modules import audit

TARGET = "platform_owner"


def own_row(db, owner: PlatformOwner, action: str, summary: str, **kwargs) -> None:
    audit.record(db, action, actor=audit.Actor.for_platform_owner(owner), isp_operator_id=None,
                 target_type=TARGET, target_id=owner.id, summary=summary, **kwargs)


async def own_row_detached(owner: PlatformOwner, action: str, summary: str, **kwargs) -> None:
    await audit.record_detached(action, actor=audit.Actor.for_platform_owner(owner), isp_operator_id=None,
                                target_type=TARGET, target_id=owner.id, summary=summary, **kwargs)


async def wrong_password(owner: PlatformOwner, action: str, what: str) -> None:
    """A signed-in owner's step refused for a wrong current password."""
    await own_row_detached(owner, action, f"{what} refused: wrong current password",
                           detail={"reason": "wrong_current_password"})


def anonymous_row(background: BackgroundTasks, owner: PlatformOwner, action: str, summary: str,
                  detail: dict | None = None) -> None:
    background.add_task(
        audit.record_detached, action, actor=audit.Actor.system(), isp_operator_id=None,
        target_type=TARGET, target_id=owner.id, summary=summary, detail=detail,
        request_info=audit.current_request_info(),
    )
