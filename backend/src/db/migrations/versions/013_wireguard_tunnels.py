from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import INET, UUID

revision = "013_wireguard_tunnels"
down_revision = "012_dynamic_radius_clients"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("routers", sa.Column("wg_enabled", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("routers", sa.Column("wg_peer_public_key", sa.String(64), nullable=True))
    op.add_column("routers", sa.Column("wg_peer_private_key_encrypted", sa.Text(), nullable=True))
    op.add_column("routers", sa.Column("wg_tunnel_ip", INET(), nullable=True))
    op.add_column("routers", sa.Column("wg_last_handshake_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("routers", sa.Column("wg_is_connected", sa.Boolean(), server_default="false", nullable=False))
    op.create_unique_constraint("uq_routers_wg_peer_public_key", "routers", ["wg_peer_public_key"])

    op.create_table(
        "wg_ip_allocations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("router_id", UUID(as_uuid=True), sa.ForeignKey("routers.id", ondelete="CASCADE"), unique=True, nullable=False),
        sa.Column("tunnel_ip", INET(), unique=True, nullable=False),
        sa.Column("allocated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("wg_ip_allocations")
    op.drop_constraint("uq_routers_wg_peer_public_key", "routers", type_="unique")
    op.drop_column("routers", "wg_is_connected")
    op.drop_column("routers", "wg_last_handshake_at")
    op.drop_column("routers", "wg_tunnel_ip")
    op.drop_column("routers", "wg_peer_private_key_encrypted")
    op.drop_column("routers", "wg_peer_public_key")
    op.drop_column("routers", "wg_enabled")
