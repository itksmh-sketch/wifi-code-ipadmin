from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "003_reseller_agent_management"
down_revision = "002_payment_transactions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE reseller_role AS ENUM ('reseller', 'town_agent')")
    op.execute("CREATE TYPE reseller_wallet_tx_type AS ENUM ('topup', 'purchase', 'commission', 'adjustment', 'refund')")
    op.execute("CREATE TYPE commission_rule_type AS ENUM ('flat', 'percentage')")

    op.create_table(
        "resellers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("phone", sa.String(64), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", postgresql.ENUM(name="reseller_role", create_type=False), nullable=False),
        sa.Column("town_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("towns.id"), nullable=True),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.id"), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_resellers_email", "resellers", ["email"], unique=True)
    op.create_index("ix_resellers_town_id", "resellers", ["town_id"])
    op.create_index("ix_resellers_site_id", "resellers", ["site_id"])
    op.create_index("ix_resellers_is_active", "resellers", ["is_active"])

    op.create_table(
        "reseller_wallets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "reseller_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("resellers.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("balance_ghs", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")),
        sa.Column("lifetime_topped_up_ghs", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")),
        sa.Column("lifetime_spent_ghs", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_reseller_wallets_reseller_id", "reseller_wallets", ["reseller_id"], unique=True)

    op.create_table(
        "reseller_wallet_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("reseller_wallets.id"), nullable=False),
        sa.Column("type", postgresql.ENUM(name="reseller_wallet_tx_type", create_type=False), nullable=False),
        sa.Column("amount_ghs", sa.Numeric(10, 2), nullable=False),
        sa.Column("balance_after_ghs", sa.Numeric(10, 2), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("reference", sa.String(255), nullable=False, unique=True),
        sa.Column("voucher_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("vouchers.id"), nullable=True),
        sa.Column("triggered_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_reseller_wallet_transactions_wallet_id_created_at",
        "reseller_wallet_transactions",
        ["wallet_id", "created_at"],
    )
    op.create_index("ix_reseller_wallet_transactions_reference", "reseller_wallet_transactions", ["reference"], unique=True)

    op.create_table(
        "commission_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("reseller_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("resellers.id"), nullable=True),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("plans.id"), nullable=True),
        sa.Column("type", postgresql.ENUM(name="commission_rule_type", create_type=False), nullable=False),
        sa.Column("value", sa.Numeric(10, 4), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_commission_rules_reseller_id", "commission_rules", ["reseller_id"])
    op.create_index("ix_commission_rules_plan_id", "commission_rules", ["plan_id"])
    op.create_index("ix_commission_rules_is_active", "commission_rules", ["is_active"])

    op.create_table(
        "reseller_voucher_allocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("reseller_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("resellers.id"), nullable=False),
        sa.Column("voucher_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("vouchers.id"), nullable=False, unique=True),
        sa.Column("allocated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sold_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sold_to_phone", sa.String(64), nullable=True),
        sa.Column("purchase_price_ghs", sa.Numeric(10, 2), nullable=False),
    )
    op.create_index("ix_reseller_voucher_allocations_reseller_id", "reseller_voucher_allocations", ["reseller_id"])
    op.create_index("ix_reseller_voucher_allocations_reseller_id_sold_at", "reseller_voucher_allocations", ["reseller_id", "sold_at"])


def downgrade() -> None:
    op.drop_index("ix_reseller_voucher_allocations_reseller_id_sold_at", table_name="reseller_voucher_allocations")
    op.drop_index("ix_reseller_voucher_allocations_reseller_id", table_name="reseller_voucher_allocations")
    op.drop_table("reseller_voucher_allocations")

    op.drop_index("ix_commission_rules_is_active", table_name="commission_rules")
    op.drop_index("ix_commission_rules_plan_id", table_name="commission_rules")
    op.drop_index("ix_commission_rules_reseller_id", table_name="commission_rules")
    op.drop_table("commission_rules")

    op.drop_index("ix_reseller_wallet_transactions_reference", table_name="reseller_wallet_transactions")
    op.drop_index(
        "ix_reseller_wallet_transactions_wallet_id_created_at",
        table_name="reseller_wallet_transactions",
    )
    op.drop_table("reseller_wallet_transactions")

    op.drop_index("ix_reseller_wallets_reseller_id", table_name="reseller_wallets")
    op.drop_table("reseller_wallets")

    op.drop_index("ix_resellers_is_active", table_name="resellers")
    op.drop_index("ix_resellers_site_id", table_name="resellers")
    op.drop_index("ix_resellers_town_id", table_name="resellers")
    op.drop_index("ix_resellers_email", table_name="resellers")
    op.drop_table("resellers")

    op.execute("DROP TYPE commission_rule_type")
    op.execute("DROP TYPE reseller_wallet_tx_type")
    op.execute("DROP TYPE reseller_role")

