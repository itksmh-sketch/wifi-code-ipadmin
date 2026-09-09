"""Operator SMS credentials — bring-your-own SMS-gateway keys, per operator.

Structural twin of ``operator_payment_credentials`` (its post-026 shape): one row
per provider (``UNIQUE(isp_operator_id, provider)``), at most one active (partial
unique index on ``is_active``), a single Fernet-encrypted JSON blob keyed by that
provider's ``provider_catalog.credential_schema`` field names.

New table + new enum only. No data migration — zero rows exist and nothing
referenced ``operator_sms*`` before this. Seeds nothing: until an operator saves
a row, per-operator voucher-SMS resolution returns None and delivery is skipped,
identical to today's ``sms_provider=''`` no-op.

``africastalking_platform`` (the platform-gateway SMS option) is deliberately NOT
in ``operator_sms_provider`` — those credentials are the platform's, not the
operator's, and would live in a separate ``platform_sms_credentials`` table.

Reversible: downgrade drops the table and the enum (the table is empty).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "028_operator_sms_credentials"
down_revision = "027_flutterwave_catalog_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE operator_sms_provider AS ENUM ('hubtel', 'africastalking')")

    op.create_table(
        "operator_sms_credentials",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "isp_operator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("isp_operators.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider",
            postgresql.ENUM(name="operator_sms_provider", create_type=False),
            nullable=False,
        ),
        # Fernet ciphertext (src.utils.encryption) wrapping
        # json.dumps({field_name: value}, sort_keys=True). Opaque column; the
        # "JSON shape" is the decrypted dict. Same discipline as routers.nas_secret.
        sa.Column("credentials_encrypted", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validation_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint(
            "isp_operator_id", "provider", name="uq_operator_sms_credentials_operator_provider"
        ),
    )

    # At most one active provider per operator — mirrors
    # uq_operator_payment_credentials_one_active from migration 026.
    op.create_index(
        "uq_operator_sms_credentials_one_active",
        "operator_sms_credentials",
        ["isp_operator_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "ix_operator_sms_credentials_isp_operator_id",
        "operator_sms_credentials",
        ["isp_operator_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_operator_sms_credentials_isp_operator_id", table_name="operator_sms_credentials")
    op.drop_index("uq_operator_sms_credentials_one_active", table_name="operator_sms_credentials")
    op.drop_table("operator_sms_credentials")
    op.execute("DROP TYPE operator_sms_provider")
