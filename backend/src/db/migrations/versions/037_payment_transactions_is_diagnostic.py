"""Add payment_transactions.is_diagnostic, defaulting false.

An explicit boolean for marking a transaction as an internal/test artifact —
meant to be set only by deliberate action, never inferred from
failure_reason text matching. A text heuristic would be fragile by
construction: failure_reason is populated from real payment-provider error
messages for genuine operator-facing failures too (see paystack.py,
flutterwave.py, mtn.py), so matching on its wording risks false positives
against real transactions, and provider wording can change without notice.

No existing process sets this column today. A repo-wide search turned up no
void/cleanup script and no prior diagnostic-marking mechanism at all — this
migration adds the field for one to be built against, not to formalize an
existing one.

Additive and safe: nullable=False with a server_default, so every existing
row backfills to false (not diagnostic) with no data migration needed.
"""
from alembic import op
import sqlalchemy as sa

revision = "037_payment_transactions_is_diagnostic"
down_revision = "036_arkesel_platform_is_integrated"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payment_transactions",
        sa.Column("is_diagnostic", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("payment_transactions", "is_diagnostic")
