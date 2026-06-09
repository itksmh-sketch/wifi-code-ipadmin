from alembic import op


revision = "009_restore_active_status"
down_revision = "008_voucher_status_used"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block when it is
    # actually adding a new value.  Commit first, run the DDL, then open a new
    # transaction so the subsequent UPDATEs are still atomic.
    op.execute("COMMIT")
    op.execute("ALTER TYPE voucher_status ADD VALUE IF NOT EXISTS 'active'")
    op.execute("BEGIN")

    # Convert 'used' rows: those with an activation timestamp become 'active';
    # those without one revert to 'unused'.
    op.execute(
        "UPDATE vouchers SET status = 'active' "
        "WHERE status::text = 'used' AND activated_at IS NOT NULL"
    )
    op.execute(
        "UPDATE vouchers SET status = 'unused' "
        "WHERE status::text = 'used' AND activated_at IS NULL"
    )


def downgrade() -> None:
    # PostgreSQL cannot remove enum values, so use rename-and-recreate to drop 'active'.
    op.execute("ALTER TYPE voucher_status RENAME TO voucher_status_old")
    op.execute("CREATE TYPE voucher_status AS ENUM ('unused', 'used', 'exhausted', 'expired', 'disabled')")
    op.execute("ALTER TABLE vouchers ALTER COLUMN status DROP DEFAULT")
    op.execute(
        """
        ALTER TABLE vouchers
        ALTER COLUMN status TYPE voucher_status
        USING (
            CASE
                WHEN status::text = 'active' THEN 'used'
                ELSE status::text
            END
        )::voucher_status
        """
    )
    op.execute("ALTER TABLE vouchers ALTER COLUMN status SET DEFAULT 'unused'")
    op.execute("DROP TYPE voucher_status_old")
