"""Scope session identity to the issuing router (phase A — backward compatible).

MikroTik Acct-Session-Ids are small per-router counters ("80000000", "80100000",
...) whose high nibbles bump on reboot. They repeat across routers and across
reboots, so the old global UNIQUE(session_id) made one router's reconnect
collide with another router's stale row: the START was swallowed by
ON CONFLICT DO NOTHING and the later interim/stop wrote onto the wrong row.

This migration is deliberately ADDITIVE. It leaves the old constraint and the
old update_voucher_usage(text) in place so the currently-running FreeRADIUS
config keeps working until it is restarted onto the new sql.conf. Phase B
(046) removes them, and must run only AFTER that restart.
"""
from alembic import op
import sqlalchemy as sa


revision = "045_session_router_scoped_id"
down_revision = "044_platform_notification_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Router-scoped identity. Coexists with sessions_session_id_key for now.
    op.create_unique_constraint(
        "uq_sessions_router_session", "sessions", ["router_id", "session_id"]
    )

    # Watchdog support: there is currently no way to tell a live session from one
    # that stopped reporting months ago — upload/download bytes are overwritten
    # in place with no timestamp.
    op.add_column("sessions", sa.Column("last_interim_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE sessions SET last_interim_at = COALESCE(stopped_at, started_at) "
        "WHERE last_interim_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sessions_open_last_interim "
        "ON sessions (last_interim_at) WHERE stopped_at IS NULL"
    )

    # New overload keyed on sessions.id (uuid), which is unambiguous by
    # construction. The text overload stays until phase B.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION update_voucher_usage(target_session_id uuid)
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
            target_voucher_id uuid;
        BEGIN
            SELECT voucher_id
            INTO target_voucher_id
            FROM sessions
            WHERE id = target_session_id;

            IF target_voucher_id IS NULL THEN
                RETURN;
            END IF;

            UPDATE vouchers
            SET data_used_mb = (
                SELECT COALESCE(SUM(upload_bytes + download_bytes), 0) / 1048576
                FROM sessions
                WHERE voucher_id = target_voucher_id
            )
            WHERE id = target_voucher_id;
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS update_voucher_usage(uuid)")
    op.execute("DROP INDEX IF EXISTS ix_sessions_open_last_interim")
    op.drop_column("sessions", "last_interim_at")
    op.drop_constraint("uq_sessions_router_session", "sessions", type_="unique")
