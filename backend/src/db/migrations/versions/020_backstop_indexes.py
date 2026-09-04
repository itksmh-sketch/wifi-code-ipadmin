"""Add indexes to support the check_exhausted_sessions backstop job.

Three partial indexes:
  - sessions(voucher_id) WHERE stopped_at IS NULL
      Restricts the detection query scan to open sessions only.
  - vouchers(status) WHERE status IN ('exhausted', 'expired')
      Fast filter for terminal-status vouchers.
  - coa_events(voucher_id, status, attempt_count) WHERE event_type='disconnect'
      Makes the anti-overlap NOT EXISTS subquery fast.

All three are created with CONCURRENTLY so they do not block reads or writes on
the live production DB.  CONCURRENTLY cannot run inside a transaction, so this
migration explicitly commits the Alembic transaction before each statement.  It
is therefore NOT fully transactional — a mid-run failure leaves whichever indexes
were already built in place.  All statements use IF NOT EXISTS so re-running is
safe.
"""
from alembic import op
from sqlalchemy import text

revision = "020_backstop_indexes"
down_revision = "019_backfill_nas_secret_plain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # End the Alembic transaction — required for CONCURRENTLY.
    bind.execute(text("COMMIT"))

    bind.execute(text(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_sessions_voucher_id_open "
        "ON sessions(voucher_id) WHERE stopped_at IS NULL"
    ))
    bind.execute(text(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_vouchers_terminal_status "
        "ON vouchers(status) WHERE status IN ('exhausted', 'expired')"
    ))
    bind.execute(text(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_coa_events_backstop "
        "ON coa_events(voucher_id, status, attempt_count) "
        "WHERE event_type = 'disconnect'"
    ))


def downgrade() -> None:
    op.drop_index("ix_sessions_voucher_id_open", table_name="sessions")
    op.drop_index("ix_vouchers_terminal_status", table_name="vouchers")
    op.drop_index("ix_coa_events_backstop", table_name="coa_events")
