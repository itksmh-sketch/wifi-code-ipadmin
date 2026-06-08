from alembic import op


revision = "008_voucher_status_used"
down_revision = "007_radius_accounting_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE vouchers v
        SET
            activated_at = COALESCE(v.activated_at, v.created_at, NOW()),
            expires_at = COALESCE(
                v.expires_at,
                CASE
                    WHEN p.duration_minutes IS NOT NULL AND p.type IN ('time', 'hybrid')
                    THEN COALESCE(v.activated_at, v.created_at, NOW()) + (p.duration_minutes * INTERVAL '1 minute')
                    ELSE v.expires_at
                END
            )
        FROM plans p
        WHERE v.plan_id = p.id
        AND v.status = 'active'
        """
    )
    op.execute("UPDATE vouchers SET expires_at = NULL WHERE status = 'unused'")
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


def downgrade() -> None:
    op.execute("ALTER TYPE voucher_status RENAME TO voucher_status_old")
    op.execute("CREATE TYPE voucher_status AS ENUM ('unused', 'active', 'exhausted', 'expired', 'disabled')")
    op.execute("ALTER TABLE vouchers ALTER COLUMN status DROP DEFAULT")
    op.execute(
        """
        ALTER TABLE vouchers
        ALTER COLUMN status TYPE voucher_status
        USING (
            CASE
                WHEN status::text = 'used' THEN 'active'
                ELSE status::text
            END
        )::voucher_status
        """
    )
    op.execute("ALTER TABLE vouchers ALTER COLUMN status SET DEFAULT 'unused'")
    op.execute("DROP TYPE voucher_status_old")
