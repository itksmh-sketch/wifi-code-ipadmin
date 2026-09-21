"""Admin PIN, login/PIN lockouts, and a security-event audit trail.

Three things, all scoped to operator admins (admin_users) — platform owners are
deliberately untouched:

1. admin_users gains a PIN (bcrypt, same CryptContext as the password) plus a
   server-side elevation timestamp. pin_verified_until is a stored timestamp
   rather than a JWT claim on purpose: a claim would force a token re-issue on
   every PIN entry (the frontend has no refresh loop — it reads the token from
   localStorage once), and could not be revoked early without bumping
   token_version, which would sign the admin out entirely. Redis was rejected
   for the same reason otp.py gives for not trusting it with OTP attempts —
   middleware/rate_limit.py fails open by design, and a gate that opens when
   Redis hiccups is not a gate.

2. Two independent failure counters + lock timestamps. Password login and PIN
   entry have different thresholds (5 vs 10) and must reset independently, so
   they cannot share a column. Thresholds and the 3h duration live in code as
   constants, matching SECURITY_ANSWER_MAX_ATTEMPTS / OTP_MAX_ATTEMPTS.

3. admin_security_events — the rolling-window store behind "3 phone changes per
   30 days", and the audit trail for lockouts and PIN changes. A DB table, not
   a Redis key: a 30-day policy enforced by a fail-open cache is not a policy.
   Shaped after admin_password_reset_events (same FK pair, same
   sms_sent/sms_error pair) because it serves the same two jobs.

ENUM SEQUENCING — READ BEFORE WRITING MIGRATION 048
---------------------------------------------------
This migration runs ALTER TYPE admin_otp_purpose ADD VALUE 'phone_change'.
Verified against PostgreSQL 15.18 (what this platform runs):

  * ADD VALUE inside a transaction block is ALLOWED on PG 12+.
  * USING the new value in that same transaction is NOT: it raises
    'unsafe use of new value "phone_change" of enum type admin_otp_purpose',
    and that error rolls back the ADD VALUE along with everything else.

migrations/env.py opens ONE transaction around the whole run
(context.begin_transaction() wrapping run_migrations()), not one per revision.
So the constraint is not "within this file" — it is "within this
`alembic upgrade` invocation". This file is safe because it only adds the
label and never writes a row carrying it; runtime does that in its own
transaction later. But a future migration that INSERTs or UPDATEs an
admin_otp_codes row with purpose='phone_change' will fail whenever it is
applied in the same batch as this one — i.e. on every fresh database. If you
need that, do the data step in a separate `alembic upgrade` invocation, or
write the value with a text cast against a re-read type.

Revision ID: 047_admin_pin_and_security_events
Revises: 046_drop_global_session_id_key
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "047_admin_pin_and_security_events"
down_revision = "046_drop_global_session_id_key"
branch_labels = None
depends_on = None


def upgrade():
    # ── PIN ────────────────────────────────────────────────────────────────
    # NULL pin_hash = no PIN set yet. Every existing admin starts here, so the
    # first visit to a gated area prompts "set a PIN" rather than locking them
    # out of Payments.
    op.add_column("admin_users", sa.Column("pin_hash", sa.Text(), nullable=True))
    op.add_column("admin_users", sa.Column("pin_set_at", sa.DateTime(timezone=True), nullable=True))
    # Fixed 15-minute window from the moment of verification, not sliding.
    op.add_column("admin_users", sa.Column("pin_verified_until", sa.DateTime(timezone=True), nullable=True))

    # ── Lockouts (two independent mechanisms) ──────────────────────────────
    op.add_column("admin_users", sa.Column("pin_attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("admin_users", sa.Column("pin_locked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("admin_users", sa.Column("login_attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("admin_users", sa.Column("login_locked_until", sa.DateTime(timezone=True), nullable=True))

    # ── Security event trail ───────────────────────────────────────────────
    op.create_table(
        "admin_security_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "admin_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("isp_operator_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("isp_operators.id"), nullable=False),
        # "phone_changed" | "pin_set" | "pin_changed" | "pin_lockout" |
        # "login_lockout" | "security_question_changed". Left as a plain string
        # with no CHECK, like admin_password_reset_events.mode: the list grows,
        # and a CHECK would mean a migration every time it does.
        sa.Column("event_type", sa.String(40), nullable=False),
        # Masked/non-secret context only — e.g. {"old_phone": "233****789",
        # "new_phone": "233****321", "client_ip": "..."}. Never a PIN, code or
        # hash.
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("sms_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sms_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    # Serves the quota query verbatim:
    #   SELECT count(*) FROM admin_security_events
    #    WHERE admin_user_id = :id AND event_type = 'phone_changed'
    #      AND created_at > now() - interval '30 days'
    op.create_index(
        "ix_admin_security_events_admin_type_created",
        "admin_security_events",
        ["admin_user_id", "event_type", sa.text("created_at DESC")],
    )

    # ── OTP purpose ────────────────────────────────────────────────────────
    # Phone re-verification cannot reuse purpose='onboarding': issue_code()
    # voids every open code of the same purpose, so it would cancel a genuine
    # onboarding code mid-flow. See the ENUM SEQUENCING note above — adding the
    # label here is safe, using it here would not be.
    op.execute("ALTER TYPE admin_otp_purpose ADD VALUE IF NOT EXISTS 'phone_change'")


def downgrade():
    op.drop_index("ix_admin_security_events_admin_type_created", table_name="admin_security_events")
    op.drop_table("admin_security_events")
    op.drop_column("admin_users", "login_locked_until")
    op.drop_column("admin_users", "login_attempt_count")
    op.drop_column("admin_users", "pin_locked_until")
    op.drop_column("admin_users", "pin_attempt_count")
    op.drop_column("admin_users", "pin_verified_until")
    op.drop_column("admin_users", "pin_set_at")
    op.drop_column("admin_users", "pin_hash")
    # 'phone_change' stays on admin_otp_purpose: PostgreSQL has no
    # ALTER TYPE ... DROP VALUE, and rebuilding the type would fail against any
    # admin_otp_codes row still carrying the label. An unused label is inert, so
    # this downgrade is intentionally not a perfect inverse.
