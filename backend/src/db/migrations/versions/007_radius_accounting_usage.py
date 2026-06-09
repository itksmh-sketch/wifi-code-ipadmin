from alembic import op


revision = "007_radius_accounting_usage"
down_revision = "006_paystack_flow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION update_voucher_usage(target_session_id text)
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
            target_voucher_id uuid;
        BEGIN
            SELECT voucher_id
            INTO target_voucher_id
            FROM sessions
            WHERE session_id = target_session_id
            LIMIT 1;

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
    op.drop_index("ix_sessions_session_id", table_name="sessions")
    op.create_unique_constraint("sessions_session_id_key", "sessions", ["session_id"])


def downgrade() -> None:
    op.drop_constraint("sessions_session_id_key", "sessions", type_="unique")
    op.create_index("ix_sessions_session_id", "sessions", ["session_id"])
    op.execute("DROP FUNCTION IF EXISTS update_voucher_usage(text)")
