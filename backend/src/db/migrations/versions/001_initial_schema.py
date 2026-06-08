from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enums
    op.execute("CREATE TYPE plan_type AS ENUM ('time', 'data', 'hybrid')")
    op.execute("CREATE TYPE voucher_status AS ENUM ('unused', 'used', 'exhausted', 'expired', 'disabled')")
    op.execute("CREATE TYPE device_policy AS ENUM ('single', 'multi')")
    op.execute("CREATE TYPE admin_role AS ENUM ('superadmin', 'admin', 'viewer')")

    # towns
    op.create_table(
        "towns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("region", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # sites
    op.create_table(
        "sites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("town_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("towns.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_sites_town_id", "sites", ["town_id"])

    # routers
    op.create_table(
        "routers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("ip_address", postgresql.INET(), nullable=False),
        sa.Column("nas_identifier", sa.String(255), unique=True, nullable=False),
        sa.Column("nas_secret", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_routers_site_id", "routers", ["site_id"])

    # plans
    op.create_table(
        "plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.id"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", postgresql.ENUM(name="plan_type", create_type=False), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("data_limit_mb", sa.Integer(), nullable=True),
        sa.Column("download_speed_kbps", sa.Integer(), nullable=False),
        sa.Column("upload_speed_kbps", sa.Integer(), nullable=False),
        sa.Column("price_ghs", sa.Numeric(10, 2), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_plans_site_id", "plans", ["site_id"])

    # vouchers
    op.create_table(
        "vouchers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.id"), nullable=True),
        sa.Column("code", sa.String(255), unique=True, nullable=False),
        sa.Column("username", sa.String(255), unique=True, nullable=False),
        sa.Column("password", sa.Text(), nullable=False),
        sa.Column("status", postgresql.ENUM(name="voucher_status", create_type=False), nullable=False, server_default=sa.text("'unused'")),
        sa.Column("device_policy", postgresql.ENUM(name="device_policy", create_type=False), nullable=False, server_default=sa.text("'single'")),
        sa.Column("max_devices", sa.Integer(), server_default=sa.text("1")),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_used_mb", sa.Integer(), server_default=sa.text("0")),
        sa.Column("batch_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_vouchers_plan_id", "vouchers", ["plan_id"])
    op.create_index("ix_vouchers_site_id", "vouchers", ["site_id"])
    op.create_index("ix_vouchers_status", "vouchers", ["status"])
    op.create_index("ix_vouchers_batch_id", "vouchers", ["batch_id"])

    # sessions
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("voucher_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("vouchers.id"), nullable=False),
        sa.Column("router_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("routers.id"), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("mac_address", postgresql.MACADDR(), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("nas_ip", postgresql.INET(), nullable=False),
        sa.Column("session_id", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminate_cause", sa.String(255), nullable=True),
        sa.Column("upload_bytes", sa.BigInteger(), server_default=sa.text("0")),
        sa.Column("download_bytes", sa.BigInteger(), server_default=sa.text("0")),
    )
    op.create_index("ix_sessions_voucher_id", "sessions", ["voucher_id"])
    op.create_index("ix_sessions_router_id", "sessions", ["router_id"])
    op.create_index("ix_sessions_session_id", "sessions", ["session_id"])
    op.create_index("ix_sessions_started_at", "sessions", ["started_at"])

    # admin_users
    op.create_table(
        "admin_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", postgresql.ENUM(name="admin_role", create_type=False), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("admin_users")
    op.drop_table("sessions")
    op.drop_table("vouchers")
    op.drop_table("plans")
    op.drop_table("routers")
    op.drop_table("sites")
    op.drop_table("towns")
    op.execute("DROP TYPE admin_role")
    op.execute("DROP TYPE device_policy")
    op.execute("DROP TYPE voucher_status")
    op.execute("DROP TYPE plan_type")
