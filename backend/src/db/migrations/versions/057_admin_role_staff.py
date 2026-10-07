"""Rename the admin role 'viewer' to 'staff'.

'viewer' was never given meaning (every admin could do everything) and no row
used it. P3 introduces real roles — superadmin, admin, staff — enforced by
src/middleware/permissions.py; 'staff' is the day-to-day voucher role.

RENAME VALUE rewrites the label in pg_enum only: no table rewrite, no row
touched. A row still holding 'viewer' would simply read 'staff' afterwards.
Self-contained on purpose: no application imports.

Revision ID: 057_admin_role_staff
Revises: 056_admin_login_disable
"""
from alembic import op

revision = "057_admin_role_staff"
down_revision = "056_admin_login_disable"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TYPE admin_role RENAME VALUE 'viewer' TO 'staff'")


def downgrade():
    # Pre-P3 code gave 'viewer' everything but six reseller routes, so a
    # 'staff' user renamed back would become a near-superadmin. Fail closed:
    # sign every staff user out and disable them (P2 'system' disable) first.
    # A superadmin re-enables them, or deletes them, after deciding.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "UPDATE admin_users SET is_active = false, disabled_at = now(), "
        "disabled_by_type = 'system', disabled_by_id = NULL, "
        "disabled_reason = 'Role staff removed by rollback of migration 057', "
        "token_version = COALESCE(token_version, 0) + 1, pin_verified_until = NULL "
        "WHERE role = 'staff' AND is_active"
    )
    op.execute("ALTER TYPE admin_role RENAME VALUE 'staff' TO 'viewer'")
