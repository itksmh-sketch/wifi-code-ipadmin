from __future__ import annotations
import re
import secrets
import string
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.models import (
    OperatorApplication,
    ISPOperator,
    AdminUser,
    OperatorBillingEvent,
    OperatorPaymentCredential,
)
from src.utils.auth import hash_password
from src.modules.billing.service import get_default_monthly_fee
from src.modules.notifications import dispatcher as notify
from src.modules.applications.schemas import ApplicationSubmit


def _generate_slug(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug or "operator"


def _generate_temp_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def _unique_slug(db: AsyncSession, base: str) -> str:
    slug = base
    counter = 1
    while True:
        exists = (
            await db.execute(select(ISPOperator).where(ISPOperator.slug == slug))
        ).scalar_one_or_none()
        if not exists:
            return slug
        slug = f"{base}-{counter}"
        counter += 1


# Application statuses that hold an email address. A rejected application
# releases it, so a rejected applicant can apply again.
EMAIL_HOLDING_APPLICATION_STATUSES = ("pending", "approved")

# All an anonymous visitor ever learns about a held email: it must not reveal
# whether the match is an operator admin or an application, or its status.
EMAIL_UNAVAILABLE_MESSAGE = (
    "This email can't be used for a new application. "
    "If you've already applied or have an account, please sign in instead."
)


class EmailUnavailableError(Exception):
    """The application email is already held. Carries no reason by design."""


def _email_in_use_stmt(email: str):
    normalized = email.strip().lower()
    admin_match = select(AdminUser.id).where(func.lower(AdminUser.email) == normalized).exists()
    application_match = (
        select(OperatorApplication.id)
        .where(
            func.lower(OperatorApplication.email) == normalized,
            OperatorApplication.status.in_(EMAIL_HOLDING_APPLICATION_STATUSES),
        )
        .exists()
    )
    # Selected as two columns rather than OR-ed, so Postgres always evaluates both
    # and response time doesn't hint at which one matched.
    return select(admin_match.label("admin_match"), application_match.label("application_match"))


async def email_in_use(db: AsyncSession, email: str) -> bool:
    """True if the email belongs to an operator admin or a pending/approved application."""
    row = (await db.execute(_email_in_use_stmt(email))).one()
    return bool(row.admin_match or row.application_match)


async def admin_email_exists(db: AsyncSession, email: str) -> bool:
    stmt = select(select(AdminUser.id).where(func.lower(AdminUser.email) == email.strip().lower()).exists())
    return bool((await db.execute(stmt)).scalar())


async def submit_application(db: AsyncSession, body: ApplicationSubmit) -> OperatorApplication:
    if await email_in_use(db, body.email):
        raise EmailUnavailableError()

    app = OperatorApplication(
        isp_name=body.isp_name,
        contact_name=body.contact_name,
        email=body.email,
        phone=body.phone,
        region=body.region,
        expected_sites=body.expected_sites,
        message=body.message,
        status="pending",
    )
    db.add(app)
    await db.commit()
    await db.refresh(app)

    # Fire-and-forget notifications (failures logged internally)
    try:
        await notify.notify_application_received(
            email=app.email,
            contact_name=app.contact_name,
            isp_name=app.isp_name,
            phone=app.phone,
        )
    except Exception:
        pass

    return app


async def approve_application(
    db: AsyncSession,
    app: OperatorApplication,
    platform_owner_id: uuid.UUID,
) -> tuple[ISPOperator, str]:
    """Returns (operator, temp_password).

    The operator's monthly fee is stamped from the platform default at approval
    time — never supplied by the caller — and stays fixed at that value.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)

    monthly_fee_ghs = await get_default_monthly_fee(db)
    base_slug = await _unique_slug(db, _generate_slug(app.isp_name))
    temp_password = _generate_temp_password()

    operator = ISPOperator(
        name=app.isp_name,
        slug=base_slug,
        contact_email=app.email,
        contact_phone=app.phone,
        status="approved",
        approved_at=now,
        approved_by_platform_owner_id=platform_owner_id,
        monthly_fee_ghs=monthly_fee_ghs,
        billing_status="trial",
        trial_ends_at=now + timedelta(days=settings.trial_days),
        onboarding_checklist={},
    )
    db.add(operator)
    await db.flush()  # get operator.id

    admin = AdminUser(
        isp_operator_id=operator.id,
        email=app.email,
        password_hash=hash_password(temp_password),
        role="superadmin",
        is_active=True,
    )
    db.add(admin)

    # Update application
    app.status = "approved"
    app.reviewed_by_platform_owner_id = platform_owner_id
    app.reviewed_at = now
    app.isp_operator_id = operator.id

    # Billing event
    event = OperatorBillingEvent(
        isp_operator_id=operator.id,
        event_type="trial_started",
        description=f"Trial started for {operator.name}. Ends {operator.trial_ends_at.date()}.",
        event_metadata={"trial_days": settings.trial_days, "monthly_fee_ghs": str(monthly_fee_ghs)},
    )
    db.add(event)

    await db.commit()
    await db.refresh(operator)

    try:
        await notify.notify_application_approved(
            email=app.email,
            phone=app.phone,
            contact_name=app.contact_name,
            isp_name=app.isp_name,
            admin_email=app.email,
            temp_password=temp_password,
            trial_days=settings.trial_days,
        )
    except Exception:
        pass

    return operator, temp_password


async def reject_application(
    db: AsyncSession,
    app: OperatorApplication,
    platform_owner_id: uuid.UUID,
    rejection_reason: str,
) -> OperatorApplication:
    now = datetime.now(timezone.utc)
    app.status = "rejected"
    app.reviewed_by_platform_owner_id = platform_owner_id
    app.reviewed_at = now
    app.rejection_reason = rejection_reason

    await db.commit()
    await db.refresh(app)

    try:
        await notify.notify_application_rejected(
            email=app.email,
            phone=app.phone,
            contact_name=app.contact_name,
            isp_name=app.isp_name,
            rejection_reason=rejection_reason,
        )
    except Exception:
        pass

    return app
