"""Captive portal wrong-code limits: their settings, and the per-site record.

/portal/authenticate now counts wrong codes ("not found" only) per device
(MAC), per client IP (a site's public NAT address) and per operator, in fixed
15-minute windows held in Redis, and refuses further tries past a limit
(src.portal.login_limits). The limits are platform settings, edited on the
platform portal's Settings page, so they can be tuned without a deploy. This
seeds them with the starting values: 5 per device, 30 per IP, and an alert at
50 per operator. ON CONFLICT DO NOTHING, like 049: a value a platform owner
already saved wins.

portal_login_failure_stats is what the tuning is done from: per operator, per
UTC day, per client IP, the failures seen, the most in one window (per IP, per
device and for the operator), and how often a window reached a limit. One
upsert per wrong code; a code that exists, a refused-but-found code and a
failed check digit write nothing.

Revision ID: 062_portal_login_failures
Revises: 061_voucher_code_key
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import INET, UUID

revision = "062_portal_login_failures"
down_revision = "061_voucher_code_key"
branch_labels = None
depends_on = None

SETTINGS = {
    "portal_login_fail_limit_mac": "5",
    "portal_login_fail_limit_ip": "30",
    "portal_login_fail_alert_operator": "50",
}


def upgrade() -> None:
    op.create_table(
        "portal_login_failure_stats",
        sa.Column("isp_operator_id", UUID(as_uuid=True), sa.ForeignKey("isp_operators.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("client_ip", INET, primary_key=True),
        sa.Column("site_id", UUID(as_uuid=True), sa.ForeignKey("sites.id", ondelete="SET NULL"), nullable=True),
        sa.Column("failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("peak_ip_window", sa.Integer, nullable=False, server_default="0"),
        sa.Column("peak_device_window", sa.Integer, nullable=False, server_default="0"),
        sa.Column("peak_operator_window", sa.Integer, nullable=False, server_default="0"),
        sa.Column("device_limit_reached", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ip_limit_reached", sa.Integer, nullable=False, server_default="0"),
        sa.Column("first_failure_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # The tuning query reads one operator's recent days.
    op.create_index("ix_portal_login_failure_stats_day", "portal_login_failure_stats", ["day"])
    for key, value in SETTINGS.items():
        op.execute(sa.text("INSERT INTO platform_settings (key, value) VALUES (:k, :v) ON CONFLICT (key) DO NOTHING")
                   .bindparams(k=key, v=value))


def downgrade() -> None:
    # Only the rows still holding the seeded value; an edited one is the
    # platform owner's, and harmless once nothing reads it.
    for key, value in SETTINGS.items():
        op.execute(sa.text("DELETE FROM platform_settings WHERE key = :k AND value = :v").bindparams(k=key, v=value))
    op.drop_index("ix_portal_login_failure_stats_day", table_name="portal_login_failure_stats")
    op.drop_table("portal_login_failure_stats")
