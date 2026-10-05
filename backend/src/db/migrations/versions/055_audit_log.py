"""audit_log: one append-only trail of who changed what, for operators and the
platform owner alike.

The existing event tables each cover one slice and stay as they are:
admin_security_events (an admin's own security actions; load-bearing for the
30-day phone-change quota), admin_password_reset_events,
platform_owner_security_events and operator_billing_events. None of them can
hold this: they require an admin or an owner as the subject, and the admin one
CASCADE-deletes with the user. Every audited action writes here, security
events included, so each audit page reads one table.

Columns
-------
isp_operator_id
    The tenant the action belongs to. NULL for platform-wide actions (an owner
    changing platform settings). Operators see their own rows only.
actor_type / actor_id / actor_label
    operator_user | platform_owner | system. actor_label is the actor's email
    at the time, so the row still reads correctly after a rename or removal.
    NULL actor_id for system actions.
action / target_type / target_id / summary
    action is a dotted name ("auth.login", "security.pin_set", "plan.update").
    target_id is text, not uuid: some targets are keyed by a slug or provider.
changes
    {field: {"before": x, "after": y}} for fields whose values may be shown,
    {field: "changed"} for everything else. Built by modules.audit.redact,
    never by hand.
detail
    Non-secret context (how a sign-out arrived, attempt counts). Passed
    through the same key-based redaction as changes.
ip / user_agent / request_id
    Where the request came from. ip is the client as uvicorn reports it (the
    proxy is trusted via --forwarded-allow-ips).

No foreign keys, on purpose. Rows can't be deleted (see below), so an FK to
isp_operators or admin_users would make those rows undeletable too, and a
CASCADE or SET NULL would itself be a DELETE or UPDATE the trigger refuses.
History has to outlive what it describes.

Append-only
-----------
The app connects as a superuser, so REVOKE UPDATE/DELETE would do nothing.
Triggers still fire for superusers, so a trigger refuses UPDATE and TRUNCATE
always, and DELETE unless the transaction has run
``SET LOCAL audit.purge = 'on'``, which only the retention job does. This stops
bugs and accidents, not someone with full control of the database role; that
needs a non-superuser app role (backlog).

Revision ID: 055_audit_log
Revises: 054_sms_messages
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "055_audit_log"
down_revision = "054_sms_messages"
branch_labels = None
depends_on = None

ACTOR_TYPES = ("operator_user", "platform_owner", "system")


def upgrade():
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("isp_operator_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_label", sa.String(255), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=True),
        sa.Column("target_id", sa.String(255), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("changes", postgresql.JSONB(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(256), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "actor_type IN (" + ", ".join(f"'{t}'" for t in ACTOR_TYPES) + ")",
            name="ck_audit_log_actor_type",
        ),
    )
    # Operator page: one tenant, newest first, keyset on (created_at, id).
    op.create_index(
        "ix_audit_log_operator_created", "audit_log", ["isp_operator_id", sa.text("created_at DESC"), sa.text("id DESC")]
    )
    # Platform page (all tenants) and the retention purge.
    op.create_index("ix_audit_log_created", "audit_log", [sa.text("created_at DESC"), sa.text("id DESC")])
    op.create_index("ix_audit_log_actor_created", "audit_log", ["actor_id", sa.text("created_at DESC")])
    op.create_index("ix_audit_log_action_created", "audit_log", ["action", sa.text("created_at DESC")])

    op.execute(
        """
        CREATE FUNCTION audit_log_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND current_setting('audit.purge', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'audit_log is append-only: % refused', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();"
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only();"
    )


def downgrade():
    # DROP TABLE is not refused by the triggers (they cover UPDATE, DELETE and
    # TRUNCATE only) and takes them with it.
    op.drop_table("audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only();")
