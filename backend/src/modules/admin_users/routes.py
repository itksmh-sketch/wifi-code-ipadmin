"""Operator user management: a superadmin manages their own operator's admins
and staff.

Every route needs the ``users`` permission (superadmin only) and every write a
recent PIN. The guards, all server-side:

  * the tenant comes from the caller's token, never from the body or path;
    a user id from another operator is a 404, like one that doesn't exist;
  * superadmins are untouchable here (the platform creates and manages them),
    and so is the caller's own account;
  * new users and role changes are admin or staff only (422 otherwise);
  * a user the platform disabled stays disabled until the platform says so.

Disabling goes through access.disable_admin (actor operator_user, sessions
ended, no SMS); every action writes an audit row in the same transaction.
There is no delete: a disabled user keeps the audit history readable.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import AdminUser
from src.middleware.auth import TenantContext, require_recent_pin
from src.middleware.permissions import USERS, require_perm
from src.modules import audit
from src.modules.admin_accounts import access
from src.modules.admin_accounts.notifications import send_temp_password_sms
from src.modules.admin_accounts.provisioning import AdminEmailInUseError, provision_operator_admin
from src.schemas import OperatorUserAccessChange, OperatorUserCreate, OperatorUserRoleUpdate
from src.utils.phone import mask_phone

router = APIRouter(prefix="/admin/users", tags=["admin-users"])

NOT_FOUND = "User not found"
NOT_YOURSELF = "You can't change your own account here. Use the Security page."
NOT_SUPERADMIN = "Superadmin accounts are managed by the platform."
PLATFORM_DISABLED = "This user's sign-in was disabled by the platform. Contact support to restore it."

_WRITE = [Depends(audit.audited), Depends(require_recent_pin)]


def _disabled_by(admin: AdminUser) -> str | None:
    return {
        access.OPERATOR_USER: "operator",
        access.PLATFORM_OWNER: "platform",
    }.get(admin.disabled_by_type, "system" if admin.disabled_by_type else None)


def _row(admin: AdminUser) -> dict:
    by = _disabled_by(admin)
    return {
        "id": str(admin.id),
        "email": admin.email,
        "role": admin.role,
        "phone": mask_phone(admin.phone),
        "phone_verified": bool(admin.phone_verified),
        "must_complete_onboarding": bool(admin.must_complete_onboarding),
        "is_active": bool(admin.is_active),
        "disabled_at": admin.disabled_at,
        "disabled_by": by,
        # The platform's reason is internal to the platform; only the
        # operator's own reasons are shown back to the operator.
        "disabled_reason": admin.disabled_reason if by == "operator" else None,
        "created_at": admin.created_at,
        "last_login_at": admin.last_login_at,
    }


async def _target(db: AsyncSession, tenant: TenantContext, user_id: uuid.UUID) -> AdminUser:
    """The user to change, locked, after every guard."""
    admin = (
        await db.execute(
            select(AdminUser)
            .where(AdminUser.id == user_id, AdminUser.isp_operator_id == tenant.isp_operator_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if admin is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    if admin.id == tenant.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NOT_YOURSELF)
    if admin.role == "superadmin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NOT_SUPERADMIN)
    return admin


@router.get("")
async def list_users(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_perm(USERS)),
):
    rank = case((AdminUser.role == "superadmin", 0), (AdminUser.role == "admin", 1), else_=2)
    rows = (
        await db.execute(
            select(AdminUser)
            .where(AdminUser.isp_operator_id == tenant.isp_operator_id)
            .order_by(rank, AdminUser.created_at)
        )
    ).scalars().all()
    return [_row(a) for a in rows]


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=_WRITE)
async def create_user(
    body: OperatorUserCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_perm(USERS)),
):
    """Add an admin or staff user. They get a temporary password by SMS and must
    verify their phone and choose a password on first sign-in."""
    try:
        admin, temp_password = await provision_operator_admin(
            db, operator_id=tenant.isp_operator_id, email=body.email, phone=body.phone, role=body.role
        )
    except AdminEmailInUseError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists")
    audit.record(
        db,
        "admin_user.create",
        target_type="admin_user",
        target_id=admin.id,
        summary=f"Added {admin.role} {admin.email}",
        detail={"role": admin.role},
    )
    await db.commit()
    await db.refresh(admin)
    # Only after the commit: never text credentials for an account that rolled back.
    sms = await send_temp_password_sms(admin, temp_password)
    return {
        **_row(admin),
        # Shown once, so the superadmin can pass it on if the SMS didn't arrive.
        "temp_password": temp_password,
        "temp_password_sms_sent": sms.success,
        "temp_password_sms_error": sms.error,
    }


@router.patch("/{user_id}", dependencies=_WRITE)
async def change_role(
    user_id: uuid.UUID,
    body: OperatorUserRoleUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_perm(USERS)),
):
    admin = await _target(db, tenant, user_id)
    if admin.role != body.role:
        previous = admin.role
        admin.role = body.role
        audit.record(
            db,
            "admin_user.role_change",
            target_type="admin_user",
            target_id=admin.id,
            summary=f"Changed {admin.email} from {previous} to {body.role}",
            changes={"role": {"from": previous, "to": body.role}},
        )
        await db.commit()
    return _row(admin)


@router.post("/{user_id}/disable", dependencies=_WRITE)
async def disable_user(
    user_id: uuid.UUID,
    body: OperatorUserAccessChange,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_perm(USERS)),
):
    """Turn off one user's sign-in and end their sessions. No SMS: notices are
    for the platform's own actions only."""
    admin = await _target(db, tenant, user_id)
    if not admin.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This user's sign-in is already disabled.")
    await access.disable_admin(db, admin, by_type=access.OPERATOR_USER, by_id=tenant.user_id, reason=body.reason)
    await db.commit()
    return _row(admin)


@router.post("/{user_id}/enable", dependencies=_WRITE)
async def enable_user(
    user_id: uuid.UUID,
    body: OperatorUserAccessChange,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_perm(USERS)),
):
    admin = await _target(db, tenant, user_id)
    if admin.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This user's sign-in is not disabled.")
    if admin.disabled_by_type != access.OPERATOR_USER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PLATFORM_DISABLED)
    access.enable_admin(db, admin, reason=body.reason)
    await db.commit()
    return _row(admin)
