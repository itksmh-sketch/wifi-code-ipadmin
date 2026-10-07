"""Operator-admin permissions: one role -> permissions map, one dependency.

Every operator-admin route declares exactly one permission with
``require_perm`` (tests/test_permissions.py walks the live route table and
fails on a route with none, or more than one). The server is the authority;
GET /auth/me hands the same set to the SPA so it can hide what would 403.

Groups, as approved for P3:
  self              G0 own account: password, PIN, phone, security question
  read              G1 dashboards, lists and details; the suspension banner
  vouchers.operate  G2 print batches, disable/reactivate, disconnect a user
  vouchers.generate    create sellable vouchers
  config            G3 plans, routers, towns/sites, branding, templates, resellers
  money             G4 refund, verify, reconcile, export, reseller top-up
  billing.read         invoices, transactions, SMS usage
  secrets           G5 payment/SMS credentials, NAS-secret hint, paying invoices
  users             G6 operator user management
  audit             G6 the operator audit log (pages arrive in P5)

An unknown role (anything not in ROLE_PERMISSIONS) gets nothing.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import AdminUser
from src.middleware.auth import (
    TenantContext,
    assert_operator_not_suspended,
    get_authenticated_admin,
    get_current_user,
)

SELF = "self"
READ = "read"
VOUCHERS_OPERATE = "vouchers.operate"
VOUCHERS_GENERATE = "vouchers.generate"
CONFIG = "config"
MONEY = "money"
BILLING_READ = "billing.read"
SECRETS = "secrets"
USERS = "users"
AUDIT = "audit"

ALL_PERMISSIONS = frozenset({
    SELF, READ, VOUCHERS_OPERATE, VOUCHERS_GENERATE, CONFIG, MONEY, BILLING_READ, SECRETS, USERS, AUDIT,
})

_STAFF = frozenset({SELF, READ, VOUCHERS_OPERATE})
_ADMIN = _STAFF | {VOUCHERS_GENERATE, CONFIG, MONEY, BILLING_READ}
_SUPERADMIN = _ADMIN | {SECRETS, USERS, AUDIT}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "staff": _STAFF,
    "admin": _ADMIN,
    "superadmin": _SUPERADMIN,
}

PERMISSION_DENIED = "You don't have permission to do this."


def permissions_for(role: str | None) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role or "", frozenset())


def _tenant(user: AdminUser) -> TenantContext:
    return TenantContext(
        is_platform_owner=False,
        isp_operator_id=user.isp_operator_id,
        user_id=user.id,
        role=user.role,
        email=user.email,
    )


def require_perm(permission: str, *, active: bool = False, onboarding: bool = False):
    """Dependency: the caller's role holds ``permission``. Returns TenantContext.

    active      also refuse while the operator is suspended (what
                require_active_operator does), for routes that create capacity.
    onboarding  resolve through get_authenticated_admin instead of
                get_current_user: only the onboarding endpoints, which must
                stay reachable on a temp password.
    """
    if permission not in ALL_PERMISSIONS:
        raise ValueError(f"Unknown permission: {permission}")
    base = get_authenticated_admin if onboarding else get_current_user

    async def _check(user: AdminUser = Depends(base), db: AsyncSession = Depends(get_db)) -> TenantContext:
        if permission not in permissions_for(user.role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PERMISSION_DENIED)
        if active:
            await assert_operator_not_suspended(db, user.isp_operator_id)
        return _tenant(user)

    _check.__name__ = f"require_perm[{permission}]"
    # Read by tests/test_permissions.py, which walks the route table.
    _check.permission = permission
    if active:
        _check.is_suspension_guard = True  # see auth.require_active_operator
    return _check
