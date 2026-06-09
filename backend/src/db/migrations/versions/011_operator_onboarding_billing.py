from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "011_operator_onboarding_billing"
down_revision = "010_multi_tenant_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New ENUM types
    op.execute("CREATE TYPE operator_application_status AS ENUM ('pending', 'approved', 'rejected')")
    op.execute(
        "CREATE TYPE operator_invoice_status AS ENUM ('draft', 'issued', 'paid', 'overdue', 'waived')"
    )
    op.execute(
        "CREATE TYPE operator_billing_event_type AS ENUM ("
        "'trial_started', 'trial_expiry_warning', 'trial_expired', "
        "'invoice_issued', 'invoice_paid', 'invoice_overdue', "
        "'grace_period_started', 'suspended', 'reactivated', 'waived')"
    )

    # operator_applications
    op.create_table(
        "operator_applications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("isp_name", sa.String(255), nullable=False),
        sa.Column("contact_name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(64), nullable=False),
        sa.Column("region", sa.String(255), nullable=False),
        sa.Column("expected_sites", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="operator_application_status", create_type=False),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("reviewed_by_platform_owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("platform_owners.id"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("isp_operator_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("isp_operators.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_operator_applications_status", "operator_applications", ["status"])
    op.create_index("ix_operator_applications_email", "operator_applications", ["email"])
    op.create_index("ix_operator_applications_created_at", "operator_applications", ["created_at"])

    # operator_invoices
    op.create_table(
        "operator_invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("isp_operator_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("isp_operators.id"), nullable=False),
        sa.Column("invoice_number", sa.String(64), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("amount_ghs", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="operator_invoice_status", create_type=False),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_reference", sa.String(255), nullable=True),
        sa.Column("paystack_payment_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("invoice_number", name="uq_operator_invoices_number"),
    )
    op.create_index("ix_operator_invoices_isp_operator_id", "operator_invoices", ["isp_operator_id"])
    op.create_index("ix_operator_invoices_status", "operator_invoices", ["status"])
    op.create_index("ix_operator_invoices_due_at", "operator_invoices", ["due_at"])

    # operator_billing_events
    op.create_table(
        "operator_billing_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("isp_operator_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("isp_operators.id"), nullable=False),
        sa.Column(
            "event_type",
            postgresql.ENUM(name="operator_billing_event_type", create_type=False),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_operator_billing_events_isp_operator_id", "operator_billing_events", ["isp_operator_id"])
    op.create_index("ix_operator_billing_events_event_type", "operator_billing_events", ["event_type"])
    op.create_index("ix_operator_billing_events_created_at", "operator_billing_events", ["created_at"])

    # Add onboarding_checklist to isp_operators
    op.add_column(
        "isp_operators",
        sa.Column("onboarding_checklist", postgresql.JSONB(), nullable=True, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("isp_operators", "onboarding_checklist")

    op.drop_index("ix_operator_billing_events_created_at", table_name="operator_billing_events")
    op.drop_index("ix_operator_billing_events_event_type", table_name="operator_billing_events")
    op.drop_index("ix_operator_billing_events_isp_operator_id", table_name="operator_billing_events")
    op.drop_table("operator_billing_events")

    op.drop_index("ix_operator_invoices_due_at", table_name="operator_invoices")
    op.drop_index("ix_operator_invoices_status", table_name="operator_invoices")
    op.drop_index("ix_operator_invoices_isp_operator_id", table_name="operator_invoices")
    op.drop_table("operator_invoices")

    op.drop_index("ix_operator_applications_created_at", table_name="operator_applications")
    op.drop_index("ix_operator_applications_email", table_name="operator_applications")
    op.drop_index("ix_operator_applications_status", table_name="operator_applications")
    op.drop_table("operator_applications")

    op.execute("DROP TYPE IF EXISTS operator_billing_event_type")
    op.execute("DROP TYPE IF EXISTS operator_invoice_status")
    op.execute("DROP TYPE IF EXISTS operator_application_status")
