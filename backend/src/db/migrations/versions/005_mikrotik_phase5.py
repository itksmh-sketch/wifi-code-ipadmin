from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "005_mikrotik_phase5"
down_revision = "004_coa_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE router_connection_status AS ENUM ('unknown', 'online', 'offline', 'auth_failed', 'timeout')")
    op.execute("CREATE TYPE router_provision_action AS ENUM ('provision', 'update_radius', 'update_hotspot', 'apply_template', 'reboot', 'diagnostics')")
    op.execute("CREATE TYPE router_provision_status AS ENUM ('pending', 'running', 'success', 'failed')")

    op.create_table(
        "router_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("router_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("routers.id"), nullable=False, unique=True),
        sa.Column("api_username", sa.String(length=255), nullable=False),
        sa.Column("api_password_encrypted", sa.Text(), nullable=False),
        sa.Column("api_port", sa.Integer(), nullable=False, server_default=sa.text("8728")),
        sa.Column("use_ssl", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "connection_status",
            postgresql.ENUM(name="router_connection_status", create_type=False),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "router_provision_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("router_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("routers.id"), nullable=False),
        sa.Column("triggered_by", sa.String(length=255), nullable=False),
        sa.Column(
            "action",
            postgresql.ENUM(name="router_provision_action", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="router_provision_status", create_type=False),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("commands_executed", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "router_metrics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("router_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("routers.id"), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("cpu_load_percent", sa.Integer(), nullable=True),
        sa.Column("memory_used_percent", sa.Integer(), nullable=True),
        sa.Column("uptime_seconds", sa.BigInteger(), nullable=True),
        sa.Column("active_sessions", sa.Integer(), nullable=True),
        sa.Column("total_tx_bytes", sa.BigInteger(), nullable=True),
        sa.Column("total_rx_bytes", sa.BigInteger(), nullable=True),
        sa.Column("board_name", sa.String(length=255), nullable=True),
        sa.Column("ros_version", sa.String(length=255), nullable=True),
    )

    op.create_table(
        "config_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("template_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_index("ix_router_metrics_router_id_collected_at", "router_metrics", ["router_id", "collected_at"])
    op.create_index("ix_router_provision_log_router_id_created_at", "router_provision_log", ["router_id", "created_at"])
    op.create_index(
        "ux_config_templates_single_default",
        "config_templates",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default = true"),
    )


def downgrade() -> None:
    op.drop_index("ux_config_templates_single_default", table_name="config_templates")
    op.drop_index("ix_router_provision_log_router_id_created_at", table_name="router_provision_log")
    op.drop_index("ix_router_metrics_router_id_collected_at", table_name="router_metrics")
    op.drop_table("config_templates")
    op.drop_table("router_metrics")
    op.drop_table("router_provision_log")
    op.drop_table("router_credentials")
    op.execute("DROP TYPE router_provision_status")
    op.execute("DROP TYPE router_provision_action")
    op.execute("DROP TYPE router_connection_status")
