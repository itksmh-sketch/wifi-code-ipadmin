from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "004_coa_events"
down_revision = "003_reseller_agent_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE coa_event_type AS ENUM ('disconnect', 'coa_update')")
    op.execute("CREATE TYPE coa_event_status AS ENUM ('pending', 'sent', 'failed', 'confirmed')")

    op.add_column("routers", sa.Column("is_online", sa.Boolean(), nullable=False, server_default=sa.text("false")))

    op.create_table(
        "coa_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=True),
        sa.Column("voucher_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("vouchers.id"), nullable=False),
        sa.Column("router_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("routers.id"), nullable=True),
        sa.Column("event_type", postgresql.ENUM(name="coa_event_type", create_type=False), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="coa_event_status", create_type=False),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_coa_events_created_at", "coa_events", ["created_at"])
    op.create_index("ix_coa_events_status", "coa_events", ["status"])
    op.create_index("ix_coa_events_voucher_id", "coa_events", ["voucher_id"])
    op.create_index("ix_coa_events_session_id", "coa_events", ["session_id"])
    op.create_index("ix_coa_events_router_id", "coa_events", ["router_id"])
    op.create_index("ix_coa_events_last_attempted_at", "coa_events", ["last_attempted_at"])

    op.create_index("ix_sessions_voucher_id_stopped_at", "sessions", ["voucher_id", "stopped_at"])
    op.create_index("ix_payment_transactions_status_initiated_at", "payment_transactions", ["status", "initiated_at"])
    op.create_index("ix_routers_is_online", "routers", ["is_online"])


def downgrade() -> None:
    op.drop_index("ix_routers_is_online", table_name="routers")
    op.drop_index("ix_payment_transactions_status_initiated_at", table_name="payment_transactions")
    op.drop_index("ix_sessions_voucher_id_stopped_at", table_name="sessions")

    op.drop_index("ix_coa_events_last_attempted_at", table_name="coa_events")
    op.drop_index("ix_coa_events_router_id", table_name="coa_events")
    op.drop_index("ix_coa_events_session_id", table_name="coa_events")
    op.drop_index("ix_coa_events_voucher_id", table_name="coa_events")
    op.drop_index("ix_coa_events_status", table_name="coa_events")
    op.drop_index("ix_coa_events_created_at", table_name="coa_events")
    op.drop_table("coa_events")
    op.drop_column("routers", "is_online")
    op.execute("DROP TYPE coa_event_status")
    op.execute("DROP TYPE coa_event_type")
