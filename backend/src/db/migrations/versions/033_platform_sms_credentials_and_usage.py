"""Add platform_sms_credentials and sms_usage_records for the platform-provided
SMS gateway (metering and billing for operators who opt into the platform's own
Arkesel account instead of bringing their own SMS credentials).

Two tables, both additive, no existing table touched:

  - platform_sms_credentials: the platform's own Arkesel keys. Structural twin
    of platform_payment_credentials (one active row at a time, enforced by a
    partial unique index on is_active) but using the newer single-blob
    encrypted-credentials shape from operator_sms_credentials /
    operator_payment_credentials, not platform_payment_credentials' older
    per-field-column shape.

  - sms_usage_records: one row per successfully-sent platform-gateway message,
    written synchronously at send time — never from a DLR webhook or a status
    poll, both unreliable for per-message billing data. segment_count is
    computed locally (GSM-7/UCS-2), never trusted from Arkesel's response.
    rate_ghs_per_segment and amount_ghs are snapshotted at send time from
    whatever provider_catalog.platform_rate_per_message was at that instant; a
    later rate change never re-prices a row already written. amount_ghs is
    rounded to the cent per-record (not at invoice-rollup time), so a monthly
    sms_usage invoice line is always the exact sum of its component records,
    not just add_line_item's quantity*unit_price recomputed after the fact.

A new platform_sms_provider enum (currently just 'arkesel') is deliberately
separate from operator_sms_provider — that enum's values are what an operator
can select (including the operator-facing 'arkesel_platform' opt-in marker
added in a later migration); this one is which underlying gateway the platform
itself is metering against. Keeping them separate means the operator-facing
selection key and the platform's own gateway identity can never be confused in
a query.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

revision = "033_platform_sms_credentials_and_usage"
down_revision = "032_portal_template_and_contact"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE platform_sms_provider AS ENUM ('arkesel')")

    op.create_table(
        "platform_sms_credentials",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "provider",
            postgresql.ENUM("arkesel", name="platform_sms_provider", create_type=False),
            nullable=False,
            server_default="arkesel",
        ),
        # Fernet token wrapping json.dumps({field_name: value}, sort_keys=True),
        # keyed by provider_catalog.credential_schema.fields[].name — same shape
        # as operator_sms_credentials.credentials_encrypted.
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
    # At most one active platform SMS credential, table-wide — there is no
    # operator scoping here, unlike operator_sms_credentials' per-operator
    # partial unique index.
    op.create_index(
        "uq_platform_sms_credentials_one_active",
        "platform_sms_credentials",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "sms_usage_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "isp_operator_id",
            UUID(as_uuid=True),
            sa.ForeignKey("isp_operators.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider",
            postgresql.ENUM("arkesel", name="platform_sms_provider", create_type=False),
            nullable=False,
            server_default="arkesel",
        ),
        sa.Column("provider_reference", sa.Text(), nullable=True),
        sa.Column("segment_count", sa.Integer(), nullable=False),
        sa.Column("rate_ghs_per_segment", sa.Numeric(10, 4), nullable=False),
        sa.Column("amount_ghs", sa.Numeric(10, 2), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # NULL until a monthly rollup folds this row into an OperatorInvoiceLineItem.
        sa.Column(
            "invoice_line_item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("operator_invoice_line_items.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("segment_count > 0", name="ck_sms_usage_records_segment_count_positive"),
    )
    op.create_index(
        "ix_sms_usage_records_operator_sent_at",
        "sms_usage_records",
        ["isp_operator_id", "sent_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sms_usage_records_operator_sent_at", table_name="sms_usage_records")
    op.drop_table("sms_usage_records")
    op.drop_index("uq_platform_sms_credentials_one_active", table_name="platform_sms_credentials")
    op.drop_table("platform_sms_credentials")
    op.execute("DROP TYPE platform_sms_provider")
