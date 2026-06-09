from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "006_paystack_flow"
down_revision = "005_mikrotik_phase5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_transactions", sa.Column("next_action", sa.String(length=64), nullable=False, server_default="wait"))
    op.add_column("payment_transactions", sa.Column("provider_state", sa.String(length=64), nullable=True))
    op.add_column("payment_transactions", sa.Column("payment_channel", sa.String(length=64), nullable=True))
    op.add_column("payment_transactions", sa.Column("display_message", sa.Text(), nullable=True))
    op.add_column("payment_transactions", sa.Column("provider_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("payment_transactions", sa.Column("last_status_check_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("payment_transactions", "last_status_check_at")
    op.drop_column("payment_transactions", "provider_payload")
    op.drop_column("payment_transactions", "display_message")
    op.drop_column("payment_transactions", "payment_channel")
    op.drop_column("payment_transactions", "provider_state")
    op.drop_column("payment_transactions", "next_action")
