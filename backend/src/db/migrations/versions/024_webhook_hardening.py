"""Webhook hardening: scoped reactivation + a record for rejected payments.

Two additive changes, both prerequisites for the platform-billing webhook fixes.

1. `isp_operators.suspension_reason` — why an operator is suspended, so a
   payment can reverse a *billing* suspension without also clearing one a
   platform owner imposed for abuse.

   An explicit column rather than inferring it from billing events, because the
   events cannot answer the question: manual suspension currently writes no
   event at all, so "the most recent `suspended` event" can be a stale billing
   one from before a manual suspension. Sequence that breaks inference:
   billing suspends (event) -> operator pays -> reactivated (event) -> platform
   owner suspends for abuse (writes nothing) -> an old invoice is paid -> the
   newest `suspended` event still says billing -> wrongly reactivated.

   NULL means "unknown", and the webhook treats unknown as *do not reactivate*.
   Deliberately not backfilled: inferring a reason for existing rows would
   reintroduce exactly the guesswork the column exists to remove. There are no
   suspended operators right now, so the fail-safe costs nothing today. A
   suspended operator with NULL simply needs reactivating by hand, which is
   visible and recoverable — unlike wrongly reactivating an abuser, which is
   silent.

2. `payment_rejected` on `operator_billing_event_type` — an underpaid or
   wrong-currency charge is recorded rather than silently dropped, so it is
   visible before the billing UI exists to surface it.

Additive only: a nullable column and a new enum value. No data is rewritten and
no existing row changes.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "024_webhook_hardening"
down_revision = "023_invoice_line_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE operator_suspension_reason AS ENUM ('billing', 'manual')")

    op.add_column(
        "isp_operators",
        sa.Column(
            "suspension_reason",
            postgresql.ENUM(name="operator_suspension_reason", create_type=False),
            nullable=True,
        ),
    )

    # PostgreSQL 12+ allows ADD VALUE inside a transaction provided the new value
    # is not *used* before commit. Nothing here uses it, so this is safe in
    # Alembic's transaction. (Server is 15.18.)
    op.execute(
        "ALTER TYPE operator_billing_event_type ADD VALUE IF NOT EXISTS 'payment_rejected'"
    )


def downgrade() -> None:
    op.drop_column("isp_operators", "suspension_reason")
    op.execute("DROP TYPE operator_suspension_reason")
    # PostgreSQL cannot remove a value from an enum; 'payment_rejected' stays.
    # Harmless — nothing references it once the code is rolled back.
