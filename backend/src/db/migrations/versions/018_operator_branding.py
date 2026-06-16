"""Add captive-portal branding columns to isp_operators.

Operators can customise the portal display name, logo, primary/accent colours and
welcome message. All columns are nullable; unset values fall back to platform
defaults at read time so an unconfigured operator's portal looks unchanged.
"""
from alembic import op
import sqlalchemy as sa

revision = "018_operator_branding"
down_revision = "017_nullable_router_ip"
branch_labels = None
depends_on = None


_COLUMNS = (
    "portal_display_name",
    "logo_url",
    "primary_color",
    "accent_color",
    "background_gradient_start",
    "portal_welcome_message",
)


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column("isp_operators", sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    for name in _COLUMNS:
        op.drop_column("isp_operators", name)
