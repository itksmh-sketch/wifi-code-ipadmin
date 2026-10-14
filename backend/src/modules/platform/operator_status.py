"""The platform owner's operator status changes: which moves are allowed and
what each one does. PUT /platform/operators/{id}/status is the only caller;
the billing system's own suspension and payment reactivation live in
jobs.billing_enforcement and billing.service.

    pending   -> approved, cancelled
    approved  -> suspended, cancelled
    suspended -> approved (unsuspend), cancelled
    cancelled -> approved (restore)

Nothing moves into 'pending' (no code path creates an operator in it any
more), and setting the current status again is refused rather than silently
rewriting it (re-suspending used to turn a billing suspension into a manual
one).

What each move does:

  * suspend: suspension_reason 'manual' (paying an invoice won't lift it) and
    invoicing paused (billing 'paused' from trial/active). Sign-in stays open
    so the operator can see why; new sales and setup changes are refused.
  * cancel: billing 'cancelled' (never invoiced again), every admin and
    reseller signed out at once and kept out (admin_accounts.access), new
    sales refused. cancelled_at and the end of the hotspot wind-down are
    recorded; Phase B enforces the wind-down in RADIUS. Data is kept.
  * unsuspend / restore: billing back to trial or active
    (billing.service.resumed_billing_status). Sessions ended by a cancellation
    stay ended. The platform's sign-in switch is a separate axis and is left
    as it is.

Every move writes one operator.status_change audit row in the caller's
transaction; the typed reason goes there only (detail.reason, which the
operator's audit view hides). The caller commits, then sends the notice this
returns. pending -> approved sends none.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ISPOperator, OperatorBillingEvent
from src.modules import audit
from src.modules.admin_accounts import access
from src.modules.audit.fields import OPERATOR_STATUS
from src.modules.billing.service import resumed_billing_status

TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"approved", "cancelled"}),
    "approved": frozenset({"suspended", "cancelled"}),
    "suspended": frozenset({"approved", "cancelled"}),
    "cancelled": frozenset({"approved"}),
}
STATUSES = frozenset(TRANSITIONS)

DEFAULT_HOTSPOT_SERVICE_DAYS = 30

SUSPENDED_NOTICE = "account_suspended_manual"
RESTORED_NOTICE = "account_restored"
CANCELLED_NOTICE = "account_cancelled"


class StatusChangeRefused(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class StatusChange:
    previous: str
    notice: str | None


async def change_status(
    db: AsyncSession,
    operator: ISPOperator,
    new_status: str,
    *,
    owner_id: uuid.UUID,
    reason: str | None,
    hotspot_service_days: int | None,
    now: datetime | None = None,
) -> StatusChange:
    """Apply one status move. Caller holds the operator row (FOR UPDATE) and
    commits. Raises StatusChangeRefused, having changed nothing, for a move
    the table above doesn't allow."""
    previous = operator.status
    if new_status not in STATUSES:
        raise StatusChangeRefused(400, "Invalid operator status")
    if new_status == previous:
        raise StatusChangeRefused(409, f"{operator.name} is already {previous}.")
    if new_status not in TRANSITIONS.get(previous, frozenset()):
        raise StatusChangeRefused(400, f"An operator can't go from {previous} to {new_status}.")
    if hotspot_service_days is not None and new_status != "cancelled":
        raise StatusChangeRefused(400, "hotspot_service_days applies only to cancellation.")

    now = now or datetime.now(timezone.utc)
    reason = access.clean_reason(reason)
    before = OPERATOR_STATUS.snapshot(operator)
    detail: dict = {"reason": reason}
    notice: str | None = None

    operator.status = new_status
    operator.updated_at = now

    if new_status == "suspended":
        operator.suspension_reason = "manual"
        if operator.billing_status in ("trial", "active"):
            operator.billing_status = "paused"
        db.add(OperatorBillingEvent(
            isp_operator_id=operator.id,
            event_type="suspended",
            description=f"{operator.name} suspended by platform owner.",
            event_metadata={"reason": "manual", "suspended_by_platform_owner_id": str(owner_id)},
        ))
        summary = f"Suspended {operator.name}"
        notice = SUSPENDED_NOTICE

    elif new_status == "cancelled":
        days = DEFAULT_HOTSPOT_SERVICE_DAYS if hotspot_service_days is None else hotspot_service_days
        operator.suspension_reason = None
        operator.billing_status = "cancelled"
        operator.cancelled_at = now
        operator.hotspot_service_ends_at = now + timedelta(days=days)
        signed_out, resellers = await access.sign_out_operator(db, operator.id)
        detail.update(hotspot_service_days=days, admins_signed_out=signed_out, resellers_blocked=resellers)
        summary = f"Cancelled {operator.name}"
        notice = CANCELLED_NOTICE

    else:  # approved
        if previous == "pending":
            summary = f"Approved {operator.name}"
        else:
            if previous == "suspended":
                operator.suspension_reason = None
                if operator.billing_status == "paused":
                    operator.billing_status = resumed_billing_status(operator, now)
                summary = f"Unsuspended {operator.name}"
                description = f"{operator.name} un-suspended by platform owner."
            else:  # cancelled
                operator.billing_status = resumed_billing_status(operator, now)
                operator.cancelled_at = None
                operator.hotspot_service_ends_at = None
                summary = f"Restored {operator.name} from cancellation"
                description = f"{operator.name} restored from cancellation by platform owner."
            db.add(OperatorBillingEvent(
                isp_operator_id=operator.id,
                event_type="reactivated",
                description=description,
                event_metadata={"reason": "manual", "previous_status": previous,
                                "reactivated_by_platform_owner_id": str(owner_id)},
            ))
            notice = RESTORED_NOTICE
        if not operator.approved_at:
            operator.approved_at = now
            operator.approved_by_platform_owner_id = owner_id

    audit.record(
        db,
        "operator.status_change",
        isp_operator_id=operator.id,
        target_type="isp_operator",
        target_id=operator.id,
        summary=summary,
        changes=OPERATOR_STATUS.diff(before, OPERATOR_STATUS.snapshot(operator)),
        detail=detail,
    )
    return StatusChange(previous=previous, notice=notice)
