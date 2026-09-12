"""Add sms_usage_records.is_diagnostic, defaulting false.

Exact mirror of migration 037's column on payment_transactions, and for the
same reason: an explicit boolean set by deliberate action, never inferred.
There is no text field on this table to heuristically match against anyway —
sms_usage_records carries no failure_reason or note column — so a marker had
to be a real field or nothing.

Needed because the two halves of a platform-gateway send live in unrelated
tables: sms_usage_records has no FK to payment_transactions (the only thing
tying them is a matching operator and a ~3-second timing window, which is
detective work, not a relationship). So marking a transaction diagnostic can
never cascade to its usage record — without this column, a usage row from a
test send is still swept into the operator's next invoice by
roll_up_sms_usage as a real charge.

roll_up_sms_usage gains a matching `is_diagnostic.is_(False)` clause in the
same change, so a flagged row is excluded from billing rather than merely
labelled.

Additive and safe: nullable=False with a server_default, so every existing
row backfills to false (not diagnostic) with no data migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "038_sms_usage_records_is_diagnostic"
down_revision = "037_payment_transactions_is_diagnostic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sms_usage_records",
        sa.Column("is_diagnostic", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("sms_usage_records", "is_diagnostic")
