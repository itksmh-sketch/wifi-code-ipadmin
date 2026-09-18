"""Make the platform's own notification texts editable.

Every operator-facing platform message — application received / approved /
rejected, trial and billing warnings, suspension, reactivation — was an
f-string in notifications/dispatcher.py or notifications/email/templates.py.
This table holds one row per (event, channel) so a platform owner can edit the
wording without a deploy.

Two columns rather than one body: an email has a plain-text part and an HTML
part and both go out in the same send, so collapsing them would silently drop
one. ``subject`` and ``body_html`` are NULL on SMS rows, which the CHECK
enforces.

Backfill imports modules.notifications.template_catalog inside upgrade()
rather than pasting the eighteen texts here. That is a deliberate break from
this directory's self-contained-snapshot habit: the catalog is also what
"reset to default" restores, and a copy in here would be a second source of
truth for the same strings, free to drift from the one the reset button uses.
The import is local to upgrade() so importing this module stays side-effect
free.

Revision ID: 044_platform_notification_templates
Revises: 043_operator_voucher_sms_template
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "044_platform_notification_templates"
down_revision = "043_operator_voucher_sms_template"
branch_labels = None
depends_on = None


def upgrade():
    templates = op.create_table(
        "platform_notification_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column(
            "updated_by_platform_owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_owners.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint("event_type", "channel", name="uq_platform_notification_templates_event_channel"),
        sa.CheckConstraint("channel IN ('email', 'sms')", name="ck_platform_notification_templates_channel"),
        sa.CheckConstraint(
            "(channel = 'sms' AND subject IS NULL AND body_html IS NULL) OR "
            "(channel = 'email' AND subject IS NOT NULL AND body_html IS NOT NULL)",
            name="ck_platform_notification_templates_email_parts",
        ),
    )

    from src.modules.notifications.template_catalog import DEFAULTS

    op.bulk_insert(
        templates,
        [
            {
                "event_type": definition.event,
                "channel": definition.channel,
                "subject": definition.subject,
                "body_text": definition.body_text,
                "body_html": definition.body_html,
            }
            for definition in DEFAULTS.values()
        ],
    )


def downgrade():
    op.drop_table("platform_notification_templates")
