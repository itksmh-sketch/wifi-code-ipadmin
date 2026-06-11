from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "014_platform_settings"
down_revision = "013_wireguard_tunnels"
branch_labels = None
depends_on = None

# Settings seeded from .env on first run; platform owner can edit later.
_SEED_KEYS = ("wg_server_endpoint", "platform_app_url", "webhook_base_url")


def upgrade() -> None:
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )

    # Seed from the current environment/config (idempotent).
    from src.config import get_settings

    settings = get_settings()
    seed = {
        "wg_server_endpoint": settings.wg_server_endpoint,
        "platform_app_url": settings.platform_app_url,
        "webhook_base_url": settings.webhook_base_url,
    }
    bind = op.get_bind()
    for key in _SEED_KEYS:
        value = seed.get(key) or ""
        bind.execute(
            text(
                "INSERT INTO platform_settings (key, value) VALUES (:k, :v) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"k": key, "v": value},
        )


def downgrade() -> None:
    op.drop_table("platform_settings")
