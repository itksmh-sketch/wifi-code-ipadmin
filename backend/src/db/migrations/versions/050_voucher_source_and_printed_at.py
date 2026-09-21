"""Record where each voucher came from, and when it was printed.

Until now nothing on a voucher said whether it was operator stock or already
sold. All three creation paths write status='unused':

  * operator batch   vouchers/engine.py        — unsold stock, safe to print
  * online purchase  payments/service.py       — created the moment payment
                                                 succeeds; already sold and
                                                 texted to the customer
  * reseller         resellers/routes.py       — allocated to (and paid for by)
                                                 a reseller

so "unused" mixed paid-for vouchers into the operator's own stock, and a print
run could hand a customer's voucher to a second buyer. batch_id prefixes
(PAY-, RES-) looked like a marker but are only a convention — the seeded
reseller stock uses SEED-, and diagnostic runs used their own.

source: varchar + CHECK rather than a Postgres ENUM. A new label is then a
constraint swap, not ALTER TYPE ... ADD VALUE with the same-transaction
restriction described in migration 047, and a label can be removed later.

Backfill follows the links, never the prefixes:
  online    a payment_transactions row points at the voucher (any tx status —
            the reversed diagnostic vouchers must not become printable stock)
  reseller  a reseller_voucher_allocations row points at it
  manual    everything else
Online wins if both exist; production has no such voucher.

server_default 'manual' exists so code that predates this column keeps
inserting in the seconds between this migration and the code that sets source
explicitly on every path. Anything online/reseller created in that window is
corrected by re-running the same backfill UPDATEs, which are idempotent.

printed_at: set when an operator confirms a print run, so a second run only
picks up vouchers that have not been printed yet.

Revision ID: 050_voucher_source_and_printed_at
Revises: 049_platform_support_email_setting
"""
from alembic import op
import sqlalchemy as sa

revision = "050_voucher_source_and_printed_at"
down_revision = "049_platform_support_email_setting"
branch_labels = None
depends_on = None

SOURCES = ("manual", "online", "reseller")

# Kept as module constants so the post-deploy correction can run the exact
# same statements (see the docstring).
BACKFILL_ONLINE = (
    "UPDATE vouchers v SET source = 'online' "
    "WHERE v.source <> 'online' "
    "AND EXISTS (SELECT 1 FROM payment_transactions t WHERE t.voucher_id = v.id)"
)
BACKFILL_RESELLER = (
    "UPDATE vouchers v SET source = 'reseller' "
    "WHERE v.source = 'manual' "
    "AND EXISTS (SELECT 1 FROM reseller_voucher_allocations a WHERE a.voucher_id = v.id)"
)


def upgrade():
    op.add_column(
        "vouchers",
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
    )
    op.create_check_constraint(
        "ck_vouchers_source",
        "vouchers",
        "source IN (" + ", ".join(f"'{s}'" for s in SOURCES) + ")",
    )
    op.add_column("vouchers", sa.Column("printed_at", sa.DateTime(timezone=True), nullable=True))

    # Every existing row is 'manual' from the default; promote by link. Online
    # first, then reseller only among what is still manual, so online wins.
    op.execute(BACKFILL_ONLINE)
    op.execute(BACKFILL_RESELLER)

    # The print query: this operator's unprinted, unused stock for one plan,
    # oldest first. Partial, so it only ever holds printable vouchers.
    op.create_index(
        "ix_vouchers_printable",
        "vouchers",
        ["isp_operator_id", "plan_id", "created_at"],
        postgresql_where=sa.text("source = 'manual' AND status = 'unused' AND printed_at IS NULL"),
    )


def downgrade():
    op.drop_index("ix_vouchers_printable", table_name="vouchers")
    op.drop_column("vouchers", "printed_at")
    op.drop_constraint("ck_vouchers_source", "vouchers", type_="check")
    op.drop_column("vouchers", "source")
