from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "002_payment_transactions"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE payment_method AS ENUM ('mtn_momo', 'vodafone_cash', 'airteltigo', 'card')")
    op.execute("CREATE TYPE payment_provider AS ENUM ('mtn', 'vodafone', 'airteltigo', 'paystack')")
    op.execute("CREATE TYPE payment_status AS ENUM ('pending', 'success', 'failed', 'refunded', 'reversed')")

    op.create_table(
        "payment_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("voucher_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("vouchers.id"), nullable=True),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("amount_ghs", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False, server_default=sa.text("'GHS'")),
        sa.Column("payment_method", postgresql.ENUM(name="payment_method", create_type=False), nullable=False),
        sa.Column("provider", postgresql.ENUM(name="payment_provider", create_type=False), nullable=False),
        sa.Column("provider_reference", sa.String(255), nullable=True),
        sa.Column("internal_reference", sa.String(255), unique=True, nullable=False),
        sa.Column("phone_number", sa.String(32), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="payment_status", create_type=False),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("initiated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("webhook_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_payment_transactions_provider_reference", "payment_transactions", ["provider_reference"])
    op.create_index("ix_payment_transactions_internal_reference", "payment_transactions", ["internal_reference"])
    op.create_index("ix_payment_transactions_status", "payment_transactions", ["status"])
    op.create_index("ix_payment_transactions_created_at", "payment_transactions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_payment_transactions_created_at", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_status", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_internal_reference", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_provider_reference", table_name="payment_transactions")
    op.drop_table("payment_transactions")
    op.execute("DROP TYPE payment_status")
    op.execute("DROP TYPE payment_provider")
    op.execute("DROP TYPE payment_method")
