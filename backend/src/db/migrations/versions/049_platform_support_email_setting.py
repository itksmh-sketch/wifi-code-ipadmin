"""Seed platform_settings.platform_support_email.

The support address shown to applicants in the application-received and
application-rejected messages was read straight from config, where it was never
changed from the placeholder support@yourplatform.com — so every rejected
applicant was pointed at an address that does not exist. It is now a platform
setting (editable in the portal's Settings page, like platform_app_url), and
this seeds it with the real address so it is correct the moment this lands,
rather than showing the placeholder until someone edits it.

ON CONFLICT DO NOTHING: if a platform owner has already saved a value through
the portal, theirs wins — a migration must never overwrite an edit.

Revision ID: 049_platform_support_email_setting
Revises: 048_admin_otp_pin_reset_purpose
"""
from alembic import op

revision = "049_platform_support_email_setting"
down_revision = "048_admin_otp_pin_reset_purpose"
branch_labels = None
depends_on = None

SUPPORT_EMAIL = "support.ipadmin@gmail.com"


def upgrade():
    op.execute(
        "INSERT INTO platform_settings (key, value) "
        f"VALUES ('platform_support_email', '{SUPPORT_EMAIL}') "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade():
    # Only remove the row if it still holds the seeded value; an address a
    # platform owner typed in afterwards is theirs, not this migration's.
    op.execute(
        "DELETE FROM platform_settings "
        f"WHERE key = 'platform_support_email' AND value = '{SUPPORT_EMAIL}'"
    )
