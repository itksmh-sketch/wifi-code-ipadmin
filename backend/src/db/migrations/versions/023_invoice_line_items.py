"""Invoice line items — invoices become itemised instead of a flat fee.

Today the only line is the monthly subscription, so every invoice has exactly
one. The point is the shape: when SMS billing lands, a platform-gateway
operator's invoice carries a `sms_usage` line alongside the `subscription` one,
priced from provider_catalog.platform_rate_per_message.

`operator_invoices.amount_ghs` stays a stored column rather than becoming a
computed read — the Paystack charge amount, the billing summary sums, the
notification templates and the operator's own invoice view all read it directly,
and none of them change. What changes is only how it is *set*: it is now the sum
of the invoice's lines, recomputed on every line write by
`billing.service.add_line_item`, instead of being assigned from monthly_fee_ghs.

`unit_price_ghs` is Numeric(10,4) to match provider_catalog.platform_rate_per_message
— GHS 0.0450/message needs 4dp — while the line total rounds to 2dp like money.

Backfill: every pre-existing invoice gets a single `subscription` line
reproducing its current amount, so no invoice is left with a total that its
lines do not account for.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "023_invoice_line_items"
down_revision = "022_platform_payment_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # sms_usage and adjustment are declared now but nothing writes them yet —
    # SMS usage billing is a later phase.
    op.execute(
        "CREATE TYPE invoice_line_item_kind AS ENUM ('subscription', 'sms_usage', 'adjustment')"
    )

    op.create_table(
        "operator_invoice_line_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            # Lines have no meaning without their invoice.
            sa.ForeignKey("operator_invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            postgresql.ENUM(name="invoice_line_item_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 4), nullable=False, server_default=sa.text("1")),
        # 4dp to match provider_catalog.platform_rate_per_message.
        sa.Column("unit_price_ghs", sa.Numeric(10, 4), nullable=False),
        # 2dp — this is money, and it is what the invoice total sums.
        sa.Column("amount_ghs", sa.Numeric(10, 2), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.CheckConstraint("quantity >= 0", name="ck_invoice_line_items_quantity_non_negative"),
    )

    # Every read of a line is "all lines for this invoice", in display order.
    op.create_index(
        "ix_invoice_line_items_invoice_sort",
        "operator_invoice_line_items",
        ["invoice_id", "sort_order"],
    )

    # Backfill: one subscription line per existing invoice, reproducing the
    # amount it already carries, so its total is fully accounted for by its
    # lines. Guarded by NOT EXISTS so a re-run adds nothing.
    op.execute(
        """
        INSERT INTO operator_invoice_line_items (
            invoice_id, kind, description, quantity, unit_price_ghs, amount_ghs, metadata, sort_order
        )
        SELECT
            i.id,
            'subscription'::invoice_line_item_kind,
            'Monthly subscription',
            1,
            i.amount_ghs,
            i.amount_ghs,
            jsonb_build_object('backfilled', true, 'migration', '023_invoice_line_items'),
            0
        FROM operator_invoices i
        WHERE NOT EXISTS (
            SELECT 1 FROM operator_invoice_line_items li WHERE li.invoice_id = i.id
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_invoice_line_items_invoice_sort", table_name="operator_invoice_line_items")
    op.drop_table("operator_invoice_line_items")
    op.execute("DROP TYPE invoice_line_item_kind")
