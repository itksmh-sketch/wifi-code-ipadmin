"""Platform's own payment credentials — the keys operators' subscriptions are collected with.

Distinct from `operator_payment_credentials`, which holds each operator's keys for
selling vouchers to their customers.  This table is platform-level: one credential
set per provider, at most one active at a time.

Mirrors the operator table's encrypted-credential pattern — Fernet via
`encrypt_secret`/`decrypt_secret`, last-4 masking on read, and
`last_validated_at`/`last_validation_error` for the test-connection endpoint.

Schema is multi-provider-capable (the enum carries every payment provider in the
catalog) while the code stays Paystack-only; adding a second provider later is a
row, not a migration.  There is no hard singleton constraint — a partial unique
index on `is_active` allows several stored credential sets with exactly one live.

Deliberately seeds nothing.  Until a row exists the resolver reads through to the
PLATFORM_BILLING_PAYSTACK_* env vars, so this migration changes no behaviour.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "022_platform_payment_credentials"
down_revision = "021_provider_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Mirrors the payment providers in provider_catalog. A new provider needs a
    # catalog row and an ALTER TYPE, but every currently-known one is here.
    op.execute(
        "CREATE TYPE platform_payment_provider AS ENUM "
        "('paystack', 'flutterwave', 'mtn_momo', 'vodafone_cash', 'airteltigo')"
    )

    op.create_table(
        "platform_payment_credentials",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "provider",
            postgresql.ENUM(name="platform_payment_provider", create_type=False),
            nullable=False,
            server_default="paystack",
        ),
        # Fernet ciphertext (src.utils.encryption). Never returned in full by the API.
        sa.Column("public_key_encrypted", sa.Text(), nullable=False),
        sa.Column("secret_key_encrypted", sa.Text(), nullable=False),
        sa.Column("webhook_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validation_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        # One stored credential set per provider; activate whichever is in use.
        sa.UniqueConstraint("provider", name="uq_platform_payment_credentials_provider"),
    )

    # At most one active row overall — the platform collects through exactly one
    # provider at a time. Partial rather than a singleton constraint, so an
    # inactive credential set can sit alongside the live one during a switchover.
    op.execute(
        "CREATE UNIQUE INDEX uq_platform_payment_credentials_active "
        "ON platform_payment_credentials (is_active) WHERE is_active"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_platform_payment_credentials_active")
    op.drop_table("platform_payment_credentials")
    op.execute("DROP TYPE platform_payment_provider")
