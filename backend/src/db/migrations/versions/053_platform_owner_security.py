"""Platform-owner account security: phone, security question, character
challenge, lockouts, OTP codes and a security event trail.

The platform owner signs in with a password alone today. This adds the state
for a second step at login, a *character challenge*: a random code shown to the
owner once at setup, of which three randomly chosen positions are asked for on
each sign-in. Also added: the phone and security question that recovery needs,
and the lockout counters that operator admins got in 047.

Nothing reads any of this until the code that uses it ships, so applying this
migration on its own changes no behaviour. That includes
``must_complete_security_setup``: it is TRUE for the existing owner from the
moment this runs (see below), but the gate that enforces it arrives in a later
change.

platform_owners columns
-----------------------
phone / phone_verified
    Same meaning as on admin_users. Only a verified phone receives codes.

security_question / security_answer_hash / security_answer_attempt_count
    Same as admin_users: the question is a key into SECURITY_QUESTIONS, the
    answer is bcrypt over normalize_answer(), and the counter locks answering
    after SECURITY_ANSWER_MAX_ATTEMPTS until a successful reset clears it.

challenge_hashes (JSONB)
    One keyed digest per character position, never the code itself:

        {"v": 1, "salt": "<hex>", "digests": ["<hex>", ...]}

    digest[i] = HMAC-SHA256(K, owner_id : salt : i : char_i), where K is
    derived from ENCRYPTION_KEY by HKDF with a fixed label. Each position can be
    checked on its own, and nothing the server stores or re-derives ever
    contains the whole code. This is keyed, not bcrypt, on purpose: one
    character from a ~30-symbol alphabet takes ~30 bcrypt calls to brute-force,
    so per-character bcrypt would give the code away to anyone holding a DB
    dump. With a key, the dump alone is not enough. A fresh ``salt`` for each
    generated code means a regenerated code shares no digests with the old one.
    NULL = no code yet.

challenge_set_at
    When the current code was generated.

challenge_pending_positions (JSONB int array) / challenge_pending_jti (uuid)
    The positions this owner is currently being asked for, and the id of the
    one login-challenge token that may answer them. The positions stay the same
    until the challenge is passed, so re-logging in cannot shop for easier
    ones. The jti changes on every password success, which makes each
    challenge token single-use and voids any earlier one.

challenge_attempt_count / challenge_locked_until
login_attempt_count / login_locked_until
    Two independent lockouts, as on admin_users (047). A lock is a timestamp
    and lapses on its own. Thresholds live in code.

must_complete_security_setup
    server_default TRUE, so the one existing owner (and any seeded later) must
    complete setup (verify phone, set security question, generate and confirm
    the code) before the challenge is enforced. Until setup is complete, login
    stays password-only, so the owner is never locked out mid-transition.

platform_owner_otp_codes
------------------------
Same shape as admin_otp_codes. It is a separate table rather than a shared one
because admin_otp_codes.admin_user_id is a NOT NULL, CASCADE FK to admin_users.
``purpose`` is a String with a CHECK constraint, not a PostgreSQL ENUM:
adding a label later is then an ordinary constraint swap. It also avoids the
"new enum value used in the same transaction" failure described in 047/048,
since env.py runs a whole upgrade batch in one transaction.

platform_owner_security_events
------------------------------
Audit trail, same idea as admin_security_events. That table can't be reused
because it requires admin_user_id and isp_operator_id. event_type is a plain
string with no CHECK, as there: the list grows.

Revision ID: 053_platform_owner_security
Revises: 052_router_metrics_health
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "053_platform_owner_security"
down_revision = "052_router_metrics_health"
branch_labels = None
depends_on = None

OTP_PURPOSES = ("setup", "reset", "challenge_reset", "phone_change")


def upgrade():
    # ── Phone + security question ──────────────────────────────────────────
    op.add_column("platform_owners", sa.Column("phone", sa.String(64), nullable=True))
    op.add_column(
        "platform_owners", sa.Column("phone_verified", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column("platform_owners", sa.Column("security_question", sa.String(64), nullable=True))
    op.add_column("platform_owners", sa.Column("security_answer_hash", sa.Text(), nullable=True))
    op.add_column(
        "platform_owners",
        sa.Column("security_answer_attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # ── Character challenge ────────────────────────────────────────────────
    op.add_column("platform_owners", sa.Column("challenge_hashes", postgresql.JSONB(), nullable=True))
    op.add_column("platform_owners", sa.Column("challenge_set_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("platform_owners", sa.Column("challenge_pending_positions", postgresql.JSONB(), nullable=True))
    op.add_column(
        "platform_owners", sa.Column("challenge_pending_jti", postgresql.UUID(as_uuid=True), nullable=True)
    )

    # ── Lockouts (two independent mechanisms) ──────────────────────────────
    op.add_column(
        "platform_owners", sa.Column("challenge_attempt_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("platform_owners", sa.Column("challenge_locked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "platform_owners", sa.Column("login_attempt_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("platform_owners", sa.Column("login_locked_until", sa.DateTime(timezone=True), nullable=True))

    # ── Setup gate ─────────────────────────────────────────────────────────
    # The TRUE default fills the existing row as the column is added, so no
    # separate UPDATE is needed to retrofit the current owner.
    op.add_column(
        "platform_owners",
        sa.Column("must_complete_security_setup", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    # ── OTP codes ──────────────────────────────────────────────────────────
    op.create_table(
        "platform_owner_otp_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "platform_owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_owners.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("phone", sa.String(64), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "purpose IN (" + ", ".join(f"'{p}'" for p in OTP_PURPOSES) + ")",
            name="ck_platform_owner_otp_codes_purpose",
        ),
    )
    op.create_index(
        "ix_platform_owner_otp_codes_owner_purpose_open",
        "platform_owner_otp_codes",
        ["platform_owner_id", "purpose", "created_at"],
        postgresql_where=sa.text("consumed_at IS NULL"),
    )

    # ── Security event trail ───────────────────────────────────────────────
    op.create_table(
        "platform_owner_security_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "platform_owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_owners.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # e.g. "login_lockout" | "challenge_lockout" | "phone_verified" |
        # "security_question_set" | "challenge_generated" | "setup_completed" |
        # "password_reset" | "challenge_reset" | "break_glass".
        sa.Column("event_type", sa.String(40), nullable=False),
        # Masked, non-secret context only — never a code, answer, position
        # digest or hash.
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("sms_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sms_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_platform_owner_security_events_owner_type_created",
        "platform_owner_security_events",
        ["platform_owner_id", "event_type", sa.text("created_at DESC")],
    )


def downgrade():
    op.drop_index(
        "ix_platform_owner_security_events_owner_type_created", table_name="platform_owner_security_events"
    )
    op.drop_table("platform_owner_security_events")
    op.drop_index("ix_platform_owner_otp_codes_owner_purpose_open", table_name="platform_owner_otp_codes")
    op.drop_table("platform_owner_otp_codes")
    for column in (
        "must_complete_security_setup",
        "login_locked_until",
        "login_attempt_count",
        "challenge_locked_until",
        "challenge_attempt_count",
        "challenge_pending_jti",
        "challenge_pending_positions",
        "challenge_set_at",
        "challenge_hashes",
        "security_answer_attempt_count",
        "security_answer_hash",
        "security_question",
        "phone_verified",
        "phone",
    ):
        op.drop_column("platform_owners", column)
