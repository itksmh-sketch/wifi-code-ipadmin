"""Add platform_notification_sms_credentials — the platform's SMS account for
notifying OPERATORS (trial expiry, invoice issued, suspension, …).

Deliberately a separate table and a separate Arkesel account from
platform_sms_credentials, which is the gateway operators resell to their own
customers and get billed per segment for. The two must not share an account:
jobs/sms_reconciliation compares the gateway account's balance draw-down
against SUM(sms_usage_records.segment_count), and notification sends write no
usage record at all. Sharing one account would make every trial-expiry warning
look like a missed metering write — permanent, growing, false drift on the one
check that exists to catch real missed writes.

Structurally identical to platform_sms_credentials (single Fernet blob, one
active row via a partial unique index, masked on read, last_validated_at /
last_validation_error), so the admin card and service layer are the same shape
as the two credential stores already built.

Reuses the existing platform_sms_provider enum rather than creating a second
one — the value means "which underlying gateway", which is the same question
here. No CREATE TYPE in this migration.

Additive: new table only, nothing existing is touched.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

revision = "039_platform_notification_sms_credentials"
down_revision = "038_sms_usage_records_is_diagnostic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_notification_sms_credentials",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "provider",
            postgresql.ENUM("arkesel", name="platform_sms_provider", create_type=False),
            nullable=False,
            server_default="arkesel",
        ),
        sa.Column("credentials_encrypted", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validation_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "uq_platform_notification_sms_credentials_one_active",
        "platform_notification_sms_credentials",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_platform_notification_sms_credentials_one_active",
        table_name="platform_notification_sms_credentials",
    )
    op.drop_table("platform_notification_sms_credentials")
