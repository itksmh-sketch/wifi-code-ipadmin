"""Per-operator purchase-confirmation SMS template.

NULL means "use the platform default" (sms.templates.DEFAULT_VOUCHER_SMS_TEMPLATE),
which is exactly the message every operator sends today — so this migration
changes no behaviour until an operator edits their text.

Stored as raw text and rendered with str.format_map against a fixed allowlist
of placeholders; it is never f-string-evaluated.

Revision ID: 043_operator_voucher_sms_template
Revises: 042_platform_owner_token_version
"""
from alembic import op
import sqlalchemy as sa

revision = "043_operator_voucher_sms_template"
down_revision = "042_platform_owner_token_version"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("isp_operators", sa.Column("voucher_sms_template", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("isp_operators", "voucher_sms_template")
