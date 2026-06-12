"""Make routers.ip_address nullable.

A router can be registered before it is reachable (behind NAT) and connected
later over a WireGuard tunnel, so it may have no direct IP address. Connectivity
then comes from the tunnel IP.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import INET

revision = "017_nullable_router_ip"
down_revision = "016_reencrypt_nas_secret"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("routers", "ip_address", existing_type=INET(), nullable=True)


def downgrade() -> None:
    # Backfill nulls before restoring NOT NULL so the constraint can be re-applied.
    op.execute("UPDATE routers SET ip_address = '0.0.0.0' WHERE ip_address IS NULL")
    op.alter_column("routers", "ip_address", existing_type=INET(), nullable=False)
