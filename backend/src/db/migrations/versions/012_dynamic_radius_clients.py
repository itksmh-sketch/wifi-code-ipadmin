from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "012_dynamic_radius_clients"
down_revision = "011_operator_onboarding_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("routers", sa.Column("nas_secret_plain", sa.String(255), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(text("SELECT id, nas_secret FROM routers")).fetchall()

    from src.utils.encryption import decrypt_secret

    for row in rows:
        try:
            plain = decrypt_secret(row.nas_secret)
        except Exception:
            # Test-data rows where nas_secret was stored as plain text
            plain = row.nas_secret
        bind.execute(
            text("UPDATE routers SET nas_secret_plain = :plain WHERE id = :id"),
            {"plain": plain, "id": str(row.id)},
        )


def downgrade() -> None:
    op.drop_column("routers", "nas_secret_plain")
