from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "015_router_setup_status"
down_revision = "014_platform_settings"
branch_labels = None
depends_on = None

# New provision-log actions so each setup section's commands are auditable in
# router_provision_log (constraint #2 — log section name + commands).
_NEW_ACTIONS = ("setup_network", "setup_hotspot", "setup_radius", "setup_nat")


def upgrade() -> None:
    # Extend the router_provision_action enum with the four setup actions.
    # ADD VALUE IF NOT EXISTS keeps the migration idempotent.
    for value in _NEW_ACTIONS:
        op.execute(f"ALTER TYPE router_provision_action ADD VALUE IF NOT EXISTS '{value}'")

    op.create_table(
        "router_setup_status",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "router_id",
            UUID(as_uuid=True),
            sa.ForeignKey("routers.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        sa.Column("network_status", sa.String(20), server_default="unconfigured", nullable=False),
        sa.Column("network_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("network_config", JSONB, nullable=True),
        sa.Column("hotspot_status", sa.String(20), server_default="unconfigured", nullable=False),
        sa.Column("hotspot_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hotspot_config", JSONB, nullable=True),
        sa.Column("radius_status", sa.String(20), server_default="unconfigured", nullable=False),
        sa.Column("radius_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("radius_config", JSONB, nullable=True),
        sa.Column("nat_status", sa.String(20), server_default="unconfigured", nullable=False),
        sa.Column("nat_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("nat_config", JSONB, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("router_setup_status")
    # NOTE: PostgreSQL cannot drop individual enum values; the four added
    # router_provision_action values are left in place on downgrade.
