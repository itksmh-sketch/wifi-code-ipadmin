"""Add the 'pin_reset' label to admin_otp_purpose.

Separate from 'reset' (password) because otp.issue_code() supersedes every open
code of the same purpose for an admin: sharing the label would make requesting
a forgot-PIN code silently cancel an in-flight forgot-password code, and vice
versa. Nothing about the two flows should interact.

This should have ridden along with 047, which added 'phone_change' — it is a
separate revision only because 047 was already applied to production.

See the ENUM SEQUENCING note in 047 before adding a migration that USES this
label: env.py wraps a whole `alembic upgrade` run in one transaction, and
PostgreSQL refuses to use an enum value added in the transaction that is still
adding it. Declaring the label here (as this does) is fine; writing a row that
carries it from a later revision applied in the same batch is not.

Revision ID: 048_admin_otp_pin_reset_purpose
Revises: 047_admin_pin_and_security_events
"""
from alembic import op

revision = "048_admin_otp_pin_reset_purpose"
down_revision = "047_admin_pin_and_security_events"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TYPE admin_otp_purpose ADD VALUE IF NOT EXISTS 'pin_reset'")


def downgrade():
    # PostgreSQL has no ALTER TYPE ... DROP VALUE, and rebuilding the type would
    # fail against any admin_otp_codes row still carrying the label. An unused
    # label is inert, so this is intentionally a no-op rather than a lie.
    pass
