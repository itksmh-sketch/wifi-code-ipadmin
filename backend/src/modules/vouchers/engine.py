import string
import random
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from src.db.models import Voucher, Plan, Site, Session, Router
from src.schemas import VoucherStatus, VoucherGenerate
from src.radius.coa_events import create_pending_disconnect_event, send_disconnect_with_event

# Valid transitions for voucher lifecycle
VALID_TRANSITIONS = {
    ("unused", "active"),
    ("active", "exhausted"),
    ("active", "expired"),
    ("unused", "disabled"),
    ("active", "disabled"),
    ("exhausted", "disabled"),
    ("expired", "disabled"),
    ("disabled", "unused"),
    ("disabled", "active"),
    ("disabled", "expired"),
    ("disabled", "exhausted"),
}


def generate_voucher_code() -> str:
    """Generate a voucher code: 4 groups of 4 uppercase alphanumeric chars."""
    chars = string.ascii_uppercase + string.digits
    groups = []
    for _ in range(4):
        group = "".join(random.choices(chars, k=4))
        groups.append(group)
    return "-".join(groups)


def generate_voucher_username() -> str:
    """Generate a unique RADIUS username (same as code for simplicity)."""
    return generate_voucher_code()


def generate_voucher_password() -> str:
    """Generate a random password for RADIUS authentication."""
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=12))


def validate_transition(current_status: str, new_status: str) -> bool:
    """Check if a status transition is valid."""
    return (current_status, new_status) in VALID_TRANSITIONS


async def transition_voucher_status(
    db: AsyncSession,
    voucher_id: uuid.UUID,
    new_status: str,
    isp_operator_id: uuid.UUID,
) -> Voucher:
    """Transition a voucher to a new status with strict state machine validation."""
    result = await db.execute(select(Voucher).where(Voucher.id == voucher_id, Voucher.isp_operator_id == isp_operator_id))
    voucher = result.scalar_one_or_none()
    if not voucher:
        raise ValueError(f"Voucher {voucher_id} not found")

    if not validate_transition(voucher.status, new_status):
        raise ValueError(
            f"Invalid transition from '{voucher.status}' to '{new_status}'"
        )

    voucher.status = new_status

    if new_status == "active" and voucher.activated_at is None:
        voucher.activated_at = datetime.now(timezone.utc)
        # Start expiry on first successful use, not at generation time.
        plan_result = await db.execute(select(Plan).where(Plan.id == voucher.plan_id, Plan.isp_operator_id == isp_operator_id))
        plan = plan_result.scalar_one_or_none()
        if plan and plan.duration_minutes is not None:
            voucher.expires_at = voucher.activated_at + timedelta(minutes=plan.duration_minutes)

    await db.commit()
    await db.refresh(voucher)
    return voucher


async def restore_voucher_status(
    db: AsyncSession,
    voucher_id: uuid.UUID,
    isp_operator_id: uuid.UUID,
) -> Voucher:
    """Re-enable a disabled voucher based on its activation and limit state."""
    result = await db.execute(select(Voucher).where(Voucher.id == voucher_id, Voucher.isp_operator_id == isp_operator_id))
    voucher = result.scalar_one_or_none()
    if not voucher:
        raise ValueError(f"Voucher {voucher_id} not found")
    if voucher.status != "disabled":
        raise ValueError(f"Invalid transition from '{voucher.status}' to restored state")

    if voucher.activated_at is None:
        return await transition_voucher_status(db, voucher_id, "unused", isp_operator_id)

    now = datetime.now(timezone.utc)
    expires_at = voucher.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < now:
            return await transition_voucher_status(db, voucher_id, "expired", isp_operator_id)

    plan_result = await db.execute(select(Plan).where(Plan.id == voucher.plan_id, Plan.isp_operator_id == isp_operator_id))
    plan = plan_result.scalar_one_or_none()
    if (
        plan
        and plan.data_limit_mb is not None
        and voucher.data_used_mb >= plan.data_limit_mb
    ):
        return await transition_voucher_status(db, voucher_id, "exhausted", isp_operator_id)

    return await transition_voucher_status(db, voucher_id, "active", isp_operator_id)


async def disable_voucher_with_disconnect(
    db: AsyncSession,
    voucher_id: uuid.UUID,
    isp_operator_id: uuid.UUID,
) -> Voucher:
    """Disable a voucher and disconnect any live session."""
    active_session_result = await db.execute(
        select(Session)
        .where(
            Session.voucher_id == voucher_id,
            Session.isp_operator_id == isp_operator_id,
            Session.stopped_at.is_(None),
        )
        .order_by(Session.started_at.desc())
        .limit(1)
    )
    active_session = active_session_result.scalar_one_or_none()

    voucher = await transition_voucher_status(db, voucher_id, "disabled", isp_operator_id)

    if not active_session:
        return voucher

    router = (
        await db.execute(select(Router).where(Router.id == active_session.router_id, Router.isp_operator_id == isp_operator_id))
    ).scalar_one_or_none()
    if not router:
        return voucher

    event = await create_pending_disconnect_event(
        db,
        isp_operator_id=isp_operator_id,
        voucher_id=voucher.id,
        router_id=router.id,
        session_row_id=active_session.id,
    )
    await send_disconnect_with_event(
        db,
        event=event,
        router=router,
        voucher=voucher,
        session=active_session,
    )
    await db.commit()
    await db.refresh(voucher)
    return voucher


async def generate_vouchers(
    db: AsyncSession,
    body: VoucherGenerate,
    isp_operator_id: uuid.UUID,
) -> Tuple[List[Voucher], str]:
    """Generate a batch of vouchers."""
    # Verify plan exists
    plan_result = await db.execute(
        select(Plan).where(Plan.id == body.plan_id, Plan.isp_operator_id == isp_operator_id, Plan.is_active == True)
    )
    plan = plan_result.scalar_one_or_none()
    if not plan:
        raise ValueError("Plan not found or is inactive")

    if body.site_id:
        site_result = await db.execute(select(Site).where(Site.id == body.site_id, Site.isp_operator_id == isp_operator_id))
        if not site_result.scalar_one_or_none():
            raise ValueError("Site not found")

    batch_id = str(uuid.uuid4())[:8]
    vouchers = []

    for _ in range(body.quantity):
        code = generate_voucher_code()
        username = generate_voucher_username()
        password = generate_voucher_password()

        voucher = Voucher(
            plan_id=body.plan_id,
            site_id=body.site_id,
            isp_operator_id=isp_operator_id,
            code=code,
            username=username,
            password=password,
            status="unused",
            device_policy=body.device_policy.value,
            max_devices=1,
            expires_at=None,
            batch_id=batch_id,
        )
        db.add(voucher)
        vouchers.append(voucher)

    await db.commit()
    for v in vouchers:
        await db.refresh(v)

    return vouchers, batch_id
