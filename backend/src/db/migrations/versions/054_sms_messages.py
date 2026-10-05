"""sms_messages: one row per SMS a provider accepted, with its delivery status.

Today an SMS is only known as "accepted by Arkesel". A purchase SMS on
2026-10-05 took 3.5 hours to arrive (54 minutes inside Arkesel, the rest on
the network), and finding that out needed a manual trace. This table keeps
each accepted message's status, read back from Arkesel's own API: on a
delivery callback (used only as a trigger, never trusted) and by a 5-minute
poll that stops after 24 hours.

Separate from sms_usage_records on purpose: that table is billing data and
covers platform-gateway purchase SMS only; this one covers every send
(operator gateways, platform notifications) and is purely operational.

Nothing reads or writes it until the code that uses it ships, so applying this
migration on its own changes no behaviour. Downgrade drops the table.

Columns
-------
isp_operator_id          null for platform-owner / operator-admin security SMS
payment_transaction_id   set for voucher-purchase SMS
purpose                  voucher_purchase | otp_* | temp_password_* | lockout_* | notification
provider                 arkesel_platform | arkesel | hubtel | africastalking
credential_scope         which key to read the status back with:
                         platform_gateway | platform_notification | operator
credential_fp            sha256[:12] of the key that sent it; a status read
                         is skipped if the current key no longer matches
sender_id                sender ID used
recipient_masked         23320****514 — never the full number
provider_reference       the provider's message id
callback_token_hash      sha256 hex of the per-message token in the
                         callback URL; null when no callback was requested
accepted_at              when the provider accepted it (our clock)
provider_status          raw provider status, e.g. SUBMITTED / DELIVERED
provider_sent_at         Arkesel sent_at_time (UTC): handed to the network
final_status_at          when we first saw a final status
status_source            callback | poll — what triggered that read
last_checked_at / check_count   poll bookkeeping

Revision ID: 054_sms_messages
Revises: 053_platform_owner_security
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "054_sms_messages"
down_revision = "053_platform_owner_security"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sms_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "isp_operator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("isp_operators.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "payment_transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payment_transactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("credential_scope", sa.String(24), nullable=False),
        sa.Column("credential_fp", sa.String(12), nullable=False),
        sa.Column("sender_id", sa.String(32), nullable=False),
        sa.Column("recipient_masked", sa.String(16), nullable=False),
        sa.Column("provider_reference", sa.Text(), nullable=True),
        sa.Column("callback_token_hash", sa.String(64), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("provider_status", sa.String(32), nullable=True),
        sa.Column("provider_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_status_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_source", sa.String(8), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("check_count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("provider", "provider_reference", name="uq_sms_messages_provider_reference"),
        sa.CheckConstraint(
            "credential_scope IN ('platform_gateway', 'platform_notification', 'operator')",
            name="ck_sms_messages_credential_scope",
        ),
        sa.CheckConstraint("status_source IN ('callback', 'poll')", name="ck_sms_messages_status_source"),
    )
    op.create_index("ix_sms_messages_operator_accepted_at", "sms_messages", ["isp_operator_id", "accepted_at"])
    # The poll's work queue: messages still without a final status.
    op.create_index(
        "ix_sms_messages_pending_accepted_at",
        "sms_messages",
        ["accepted_at"],
        postgresql_where=sa.text("final_status_at IS NULL"),
    )


def downgrade():
    op.drop_index("ix_sms_messages_pending_accepted_at", table_name="sms_messages")
    op.drop_index("ix_sms_messages_operator_accepted_at", table_name="sms_messages")
    op.drop_table("sms_messages")
