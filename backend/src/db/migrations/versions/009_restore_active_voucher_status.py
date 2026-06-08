from alembic import op


revision = "009_restore_active_status"
down_revision = "008_voucher_status_used"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE voucher_status RENAME TO voucher_status_old")
    op.execute("CREATE TYPE voucher_status AS ENUM ('unused', 'active', 'exhausted', 'expired', 'disabled')")
    op.execute("ALTER TABLE vouchers ALTER COLUMN status DROP DEFAULT")
    op.execute(
        """
        ALTER TABLE vouchers
        ALTER COLUMN status TYPE voucher_status
        USING (
            CASE
                WHEN status::text = 'used' AND activated_at IS NOT NULL THEN 'active'
                WHEN status::text = 'used' THEN 'unused'
                ELSE status::text
            END
        )::voucher_status
        """
    )
    op.execute("ALTER TABLE vouchers ALTER COLUMN status SET DEFAULT 'unused'")
    op.execute("DROP TYPE voucher_status_old")


def downgrade() -> None:
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
