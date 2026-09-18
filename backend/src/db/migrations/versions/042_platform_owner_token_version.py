"""Give platform owners the same session-invalidation handle as operator admins.

platform_owners.token_version mirrors admin_users.token_version (migration 041):
it is embedded in every platform-owner JWT, and bumping it invalidates that
owner's outstanding access and refresh tokens. Without it a platform-owner
password change could not end the sessions it was changed because of.

Purely additive: one NOT NULL column with a server default, no backfill (0 is
the correct starting value for every existing row) and no change to any
existing column.

Revision ID: 042_platform_owner_token_version
Revises: 041_admin_onboarding_and_otp
"""
from alembic import op
import sqlalchemy as sa

revision = "042_platform_owner_token_version"
down_revision = "041_admin_onboarding_and_otp"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("platform_owners", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("platform_owners", "token_version")
