from __future__ import annotations
import logging
import re
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
from src.modules.admin_accounts.notifications import send_temp_password_sms
from src.modules.admin_accounts.provisioning import provision_operator_admin
from src.modules.sms.types import SMSSendResult
from src.modules.billing.service import get_default_monthly_fee
from src.modules.notifications import dispatcher as notify
from src.modules.applications.schemas import ApplicationSubmit

logger = logging.getLogger("applications.service")

TRIAL_SETTING_KEY = "platform_trial_days"
TRIAL_DAYS_MAX = 365
_TRIAL_DAYS_FALLBACK = 14


def _coerce_trial_days(value) -> int | None:
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return days if 1 <= days <= TRIAL_DAYS_MAX else None


async def get_trial_days() -> int:
    """The free-trial length a newly approved operator is stamped with.

    Resolution order — platform_settings row (edited on the portal's Settings
    page), then TRIAL_DAYS from config/.env, then 14. The value is read once at
    approval and frozen into trial_ends_at; nothing re-derives a trial from it.

    Own short-lived session, like dispatcher._support_email: a failed lookup
    falls back to config instead of aborting the caller's approval transaction.
    """
    try:
        from src.db.base import async_session_factory
        from src.modules.platform.settings_service import get_setting

        async with async_session_factory() as db:
            stored = await get_setting(db, TRIAL_SETTING_KEY)
        resolved = _coerce_trial_days(stored)
        if resolved is not None:
            return resolved
        logger.error("trial_days_setting_invalid value=%r — using config", stored)
    except Exception as exc:
        logger.error("trial_days_lookup_failed error=%s — using config", exc)
    return _coerce_trial_days(get_settings().trial_days) or _TRIAL_DAYS_FALLBACK


def _generate_slug(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug or "operator"


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
) -> tuple[ISPOperator, str, SMSSendResult]:
    """Returns (operator, temp_password, temp-password SMS result).

    The operator's monthly fee and trial length are stamped from the platform
    defaults at approval time — never supplied by the caller — and stay fixed.
    """
    now = datetime.now(timezone.utc)

    trial_days = await get_trial_days()
    monthly_fee_ghs = await get_default_monthly_fee(db)
    base_slug = await _unique_slug(db, _generate_slug(app.isp_name))

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
        trial_ends_at=now + timedelta(days=trial_days),
        onboarding_checklist={},
    )
    db.add(operator)
    await db.flush()  # get operator.id

    admin, temp_password = await provision_operator_admin(
        db, operator_id=operator.id, email=app.email, phone=app.phone, role="superadmin"
    )

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
        event_metadata={"trial_days": trial_days, "monthly_fee_ghs": str(monthly_fee_ghs)},
    )
    db.add(event)

    await db.commit()
    await db.refresh(operator)

    # Only after the commit: never text credentials for an account that rolled back.
    sms_result = await send_temp_password_sms(admin, temp_password)

    try:
        await notify.notify_application_approved(
            email=app.email,
            phone=app.phone,
            contact_name=app.contact_name,
            isp_name=app.isp_name,
            admin_email=app.email,
            temp_password=temp_password,
            trial_days=trial_days,
            send_sms=False,
        )
    except Exception:
        pass

    return operator, temp_password, sms_result


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
