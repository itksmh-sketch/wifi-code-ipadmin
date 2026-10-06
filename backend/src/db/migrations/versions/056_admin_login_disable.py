"""Disabling sign-in: per admin, and for a whole operator.

admin_users
-----------
disabled_at / disabled_by_type / disabled_by_id / disabled_reason
    Who turned the account off, when and why. ``is_active`` stays the switch
    every query already checks; these columns explain it. by_type is
    platform_owner | operator_user | system (system only for rows that were
    already inactive before this migration). by_id is not a foreign key
    because it points at one of two tables.
is_active NOT NULL
    It was nullable with a TRUE default. Every query compares it to TRUE, so a
    NULL already behaved as inactive; NOT NULL removes the third state. Any
    NULL is backfilled TRUE first (production had none).
ck_admin_users_disabled_state
    Active rows carry no disabled_* values; inactive rows carry at least
    disabled_at and disabled_by_type. Pre-existing inactive rows are given
    disabled_at = now() and by_type 'system' so the CHECK can be added.

isp_operators
-------------
admin_logins_disabled_at / _by / _reason
    The platform's operator-wide switch. While set, no admin or reseller of
    that operator can sign in or use the API. Kept apart from ``status`` /
    suspension on purpose: suspension is about billing and must still let the
    operator sign in to pay. _by is a SET NULL foreign key to platform_owners.
    ck_isp_operators_admin_logins_disabled: _by and _reason only while _at is set.

platform_notification_templates
-------------------------------
Seeds admin_access_disabled and admin_access_restored (email + SMS). The text
is written out here rather than imported from template_catalog: a migration
must not depend on application code, which may be older or newer than it
when it runs. tests/test_migration_056_templates.py pins these rows to the
catalog's DEFAULTS byte for byte. ON CONFLICT DO NOTHING, so a re-run or a
row created by "reset to default" is left alone.

audit_log
---------
A one-time fix for rows written before the model used
JSONB(none_as_null=True): an absent changes/detail was stored as the JSON
value null instead of SQL NULL. The append-only trigger refuses UPDATE, so it
is disabled for these two statements and re-enabled straight after, all inside
this migration's transaction: if anything here fails, the whole batch rolls
back with the trigger still enabled. The ALTERs hold an exclusive lock on
audit_log until commit, so audit inserts wait for the (milliseconds-long)
migration rather than slipping past an unguarded table.

Revision ID: 056_admin_login_disable
Revises: 055_audit_log
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "056_admin_login_disable"
down_revision = "055_audit_log"
branch_labels = None
depends_on = None

NEW_EVENTS = ("admin_access_disabled", "admin_access_restored")
DISABLED_BY_TYPES = ("platform_owner", "operator_user", "system")

TEMPLATE_ROWS = (
    {
        "event_type": "admin_access_disabled",
        "channel": "email",
        "subject": "Sign-in disabled — {isp_name}",
        "body_text": (
            "Sign-in for {access_scope} at {isp_name} on {platform_name} has been disabled by the platform team.\n"
            "Your hotspot keeps running: existing vouchers and sessions are not affected.\n"
            "Questions? Email {support_email}"
        ),
        "body_html": (
            "<h2>Sign-in disabled — {isp_name}</h2>"
            "<p>Sign-in for <strong>{access_scope}</strong> at {isp_name} on <strong>{platform_name}</strong> "
            "has been disabled by the platform team.</p>"
            "<p>Your hotspot keeps running: existing vouchers and sessions are not affected.</p>"
            "<p>Questions? Email us at <a href='mailto:{support_email}'>{support_email}</a></p>"
        ),
    },
    {
        "event_type": "admin_access_disabled",
        "channel": "sms",
        "subject": None,
        "body_text": (
            "{platform_name}: Sign-in for {access_scope} at {isp_name} has been disabled. "
            "Hotspot service is not affected. Questions: {support_email}"
        ),
        "body_html": None,
    },
    {
        "event_type": "admin_access_restored",
        "channel": "email",
        "subject": "Sign-in restored — {isp_name}",
        "body_text": (
            "Sign-in for {access_scope} at {isp_name} on {platform_name} has been restored. Sign in again to continue.\n"
            "Questions? Email {support_email}"
        ),
        "body_html": (
            "<h2>Sign-in restored — {isp_name}</h2>"
            "<p>Sign-in for <strong>{access_scope}</strong> at {isp_name} on <strong>{platform_name}</strong> "
            "has been restored. Sign in again to continue.</p>"
            "<p>Questions? Email us at <a href='mailto:{support_email}'>{support_email}</a></p>"
        ),
    },
    {
        "event_type": "admin_access_restored",
        "channel": "sms",
        "subject": None,
        "body_text": "{platform_name}: Sign-in for {access_scope} at {isp_name} has been restored. Sign in again to continue.",
        "body_html": None,
    },
)


def upgrade():
    # Fail fast rather than queue behind a long-held lock on these live tables
    # (and block every login queued behind us). LOCAL: this transaction only.
    op.execute("SET LOCAL lock_timeout = '5s'")
    # ── admin_users ────────────────────────────────────────────────────────
    op.add_column("admin_users", sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("admin_users", sa.Column("disabled_by_type", sa.String(20), nullable=True))
    op.add_column("admin_users", sa.Column("disabled_by_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("admin_users", sa.Column("disabled_reason", sa.Text(), nullable=True))

    op.execute("UPDATE admin_users SET is_active = TRUE WHERE is_active IS NULL")
    op.alter_column("admin_users", "is_active", nullable=False, server_default=sa.true())
    op.execute(
        "UPDATE admin_users SET disabled_at = now(), disabled_by_type = 'system' "
        "WHERE NOT is_active AND disabled_at IS NULL"
    )
    op.create_check_constraint(
        "ck_admin_users_disabled_by_type",
        "admin_users",
        "disabled_by_type IS NULL OR disabled_by_type IN ("
        + ", ".join(f"'{t}'" for t in DISABLED_BY_TYPES) + ")",
    )
    op.create_check_constraint(
        "ck_admin_users_disabled_state",
        "admin_users",
        "(is_active AND disabled_at IS NULL AND disabled_by_type IS NULL "
        "AND disabled_by_id IS NULL AND disabled_reason IS NULL) "
        "OR (NOT is_active AND disabled_at IS NOT NULL AND disabled_by_type IS NOT NULL)",
    )

    # ── isp_operators ──────────────────────────────────────────────────────
    op.add_column("isp_operators", sa.Column("admin_logins_disabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "isp_operators",
        sa.Column(
            "admin_logins_disabled_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_owners.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("isp_operators", sa.Column("admin_logins_disabled_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_isp_operators_admin_logins_disabled",
        "isp_operators",
        "admin_logins_disabled_at IS NOT NULL "
        "OR (admin_logins_disabled_by IS NULL AND admin_logins_disabled_reason IS NULL)",
    )

    # ── notification templates ─────────────────────────────────────────────
    # An empty list would render as INSERT ... DEFAULT VALUES; refuse anything
    # but the four rows rather than seed a partial set.
    pairs = {(r["event_type"], r["channel"]) for r in TEMPLATE_ROWS}
    if len(TEMPLATE_ROWS) != 4 or pairs != {(e, c) for e in NEW_EVENTS for c in ("email", "sms")}:
        raise RuntimeError(f"056: expected 4 template rows, got {sorted(pairs)}")

    templates = sa.table(
        "platform_notification_templates",
        sa.column("event_type", sa.String),
        sa.column("channel", sa.String),
        sa.column("subject", sa.Text),
        sa.column("body_text", sa.Text),
        sa.column("body_html", sa.Text),
    )
    op.execute(
        postgresql.insert(templates)
        .values(list(TEMPLATE_ROWS))
        .on_conflict_do_nothing(index_elements=["event_type", "channel"])
    )

    # ── audit_log: JSON null -> SQL NULL ───────────────────────────────────
    op.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update_delete")
    op.execute("UPDATE audit_log SET changes = NULL WHERE changes = 'null'::jsonb")
    op.execute("UPDATE audit_log SET detail = NULL WHERE detail = 'null'::jsonb")
    op.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update_delete")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    # The audit_log normalisation is not reversed: JSON null and SQL NULL mean
    # the same thing to every reader, and restoring the former would need the
    # trigger off again for no benefit.
    op.execute(
        "DELETE FROM platform_notification_templates WHERE event_type IN ("
        + ", ".join(f"'{e}'" for e in NEW_EVENTS) + ")"
    )
    op.drop_constraint("ck_isp_operators_admin_logins_disabled", "isp_operators", type_="check")
    op.drop_column("isp_operators", "admin_logins_disabled_reason")
    op.drop_column("isp_operators", "admin_logins_disabled_by")
    op.drop_column("isp_operators", "admin_logins_disabled_at")
    op.drop_constraint("ck_admin_users_disabled_state", "admin_users", type_="check")
    op.drop_constraint("ck_admin_users_disabled_by_type", "admin_users", type_="check")
    op.alter_column("admin_users", "is_active", nullable=True, server_default=sa.true())
    op.drop_column("admin_users", "disabled_reason")
    op.drop_column("admin_users", "disabled_by_id")
    op.drop_column("admin_users", "disabled_by_type")
    op.drop_column("admin_users", "disabled_at")
