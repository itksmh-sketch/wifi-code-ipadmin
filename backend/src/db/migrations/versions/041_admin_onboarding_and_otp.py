"""Operator-admin onboarding, password reset and session invalidation.

admin_users gains:
  - phone / phone_verified: the admin's own verified mobile number (previously
    the only phone on file was the operator's billing contact_phone).
  - must_complete_onboarding: set on every account created with a temp
    password; the API refuses everything except the onboarding endpoints until
    the admin verifies a phone and sets their own password.
  - must_change_password: set when a platform owner resets the password of an
    admin whose phone is already verified; the API serves only the
    set-new-password step until they choose one (no phone/OTP step).
  - token_version: embedded in every admin JWT; bumping it invalidates all of
    that admin's outstanding access and refresh tokens.
  - security_question (a key into an in-code list — no lookup table exists
    anywhere else in this schema, and the question wording belongs with the
    code that renders it) / security_answer_hash / security_answer_attempt_count.

admin_otp_codes holds hashed one-time codes for onboarding phone verification
and password reset. ``phone`` records the number the code was sent to, so a
successful onboarding verification can store exactly the number that proved
possession.

admin_password_reset_events is the audit trail for platform-owner-initiated
resets: who reset which admin, when, how (temp password by SMS vs. back
through onboarding), and whether the SMS went out.

Backfill: every existing admin is flagged must_complete_onboarding=true (the
retrofit decision) — their phones have never been verified and none of them
has a security question. phone_verified stays false.

Revision ID: 041_admin_onboarding_and_otp
Revises: 040_router_removal_outcome
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "041_admin_onboarding_and_otp"
down_revision = "040_router_removal_outcome"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("admin_users", sa.Column("phone", sa.String(64), nullable=True))
    op.add_column("admin_users", sa.Column("phone_verified", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column(
        "admin_users",
        sa.Column("must_complete_onboarding", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "admin_users",
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("admin_users", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("admin_users", sa.Column("security_question", sa.String(64), nullable=True))
    op.add_column("admin_users", sa.Column("security_answer_hash", sa.Text(), nullable=True))
    op.add_column(
        "admin_users",
        sa.Column("security_answer_attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # Retrofit: every existing admin goes through onboarding on next login.
    op.execute("UPDATE admin_users SET must_complete_onboarding = true")

    otp_purpose = postgresql.ENUM("onboarding", "reset", name="admin_otp_purpose")
    otp_purpose.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "admin_otp_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "admin_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "purpose",
            postgresql.ENUM("onboarding", "reset", name="admin_otp_purpose", create_type=False),
            nullable=False,
        ),
        sa.Column("phone", sa.String(64), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_admin_otp_codes_admin_purpose_open",
        "admin_otp_codes",
        ["admin_user_id", "purpose", "created_at"],
        postgresql_where=sa.text("consumed_at IS NULL"),
    )

    op.create_table(
        "admin_password_reset_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "admin_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("isp_operator_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("isp_operators.id"), nullable=False),
        sa.Column(
            "platform_owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_owners.id"),
            nullable=False,
        ),
        # "temp_password" (verified phone: new temp password by SMS) or
        # "onboarding" (no verified phone: back through the full setup flow).
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("phone_changed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sms_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sms_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_admin_password_reset_events_admin", "admin_password_reset_events", ["admin_user_id", "created_at"])


def downgrade():
    op.drop_index("ix_admin_password_reset_events_admin", table_name="admin_password_reset_events")
    op.drop_table("admin_password_reset_events")
    op.drop_index("ix_admin_otp_codes_admin_purpose_open", table_name="admin_otp_codes")
    op.drop_table("admin_otp_codes")
    postgresql.ENUM(name="admin_otp_purpose").drop(op.get_bind(), checkfirst=True)
    op.drop_column("admin_users", "security_answer_attempt_count")
    op.drop_column("admin_users", "security_answer_hash")
    op.drop_column("admin_users", "security_question")
    op.drop_column("admin_users", "token_version")
    op.drop_column("admin_users", "must_change_password")
    op.drop_column("admin_users", "must_complete_onboarding")
    op.drop_column("admin_users", "phone_verified")
    op.drop_column("admin_users", "phone")
