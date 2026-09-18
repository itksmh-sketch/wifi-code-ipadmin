"""The single place an operator AdminUser row is created.

All three creation paths — application approval, a platform owner creating an
operator, and a platform owner adding an admin to an existing operator — call
provision_operator_admin(), so every new account gets the same normalization
and the same onboarding gate. It never commits and never sends anything: each
caller commits as part of its own transaction and only then sends the temp
password (see notifications.send_temp_password_sms), so an SMS is never sent
for an account whose transaction rolled back.
"""
from __future__ import annotations

import secrets
import string
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AdminUser
from src.utils.auth import hash_password
from src.utils.email_address import normalize_email
from src.utils.phone import normalize_ghana_phone

ADMIN_ROLES = ("superadmin", "admin", "viewer")


class AdminEmailInUseError(ValueError):
    pass


def generate_temp_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def admin_email_taken(db: AsyncSession, email: str) -> bool:
    """Case-insensitive: older rows were stored without normalization."""
    normalized = normalize_email(email)
    row = (
        await db.execute(select(AdminUser.id).where(func.lower(AdminUser.email) == normalized).limit(1))
    ).first()
    return row is not None


async def provision_operator_admin(
    db: AsyncSession,
    *,
    operator_id: uuid.UUID,
    email: str,
    phone: str,
    role: str,
) -> tuple[AdminUser, str]:
    """Add (not commit) a new operator admin and return it with its temp password.

    The account must complete onboarding (verify a phone, set its own password
    and security question) before the API will serve it anything else.
    """
    if role not in ADMIN_ROLES:
        raise ValueError(f"Unknown admin role: {role}")
    normalized_email = normalize_email(email)
    if await admin_email_taken(db, normalized_email):
        raise AdminEmailInUseError("An admin account with this email already exists")

    temp_password = generate_temp_password()
    admin = AdminUser(
        isp_operator_id=operator_id,
        email=normalized_email,
        password_hash=hash_password(temp_password),
        role=role,
        is_active=True,
        phone=normalize_ghana_phone(phone),
        phone_verified=False,
        must_complete_onboarding=True,
        token_version=0,
        security_answer_attempt_count=0,
    )
    db.add(admin)
    await db.flush()
    return admin, temp_password
