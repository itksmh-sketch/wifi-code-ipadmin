"""Add captive-portal template selection and contact-footer fields.

Both additive, both nullable — same pattern as migration 018's branding columns,
so an operator with nothing configured renders identically to today:

  - portal_template: which structural layout renders the captive portal
    (card_centered | full_bleed). NULL resolves to "card_centered" (today's
    layout) at read time in build_branding, not enforced here, so existing rows
    need no backfill.
  - portal_contact_phone / portal_contact_email: footer contact details,
    rendered by the shared branding.js module on login/pay/success. Named with
    the portal_ prefix (not contact_phone/contact_email) because isp_operators
    already has contact_email/contact_phone as the operator's account/billing
    contact from signup — reusing those names would collide with that mapped
    SQLAlchemy attribute. NULL on either (or both) keeps the existing generic
    footer text.
"""
from alembic import op
import sqlalchemy as sa

revision = "032_portal_template_and_contact"
down_revision = "031_arkesel_sms_provider"
branch_labels = None
depends_on = None


_COLUMNS = (
    "portal_template",
    "portal_contact_phone",
    "portal_contact_email",
)


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column("isp_operators", sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    for name in _COLUMNS:
        op.drop_column("isp_operators", name)
