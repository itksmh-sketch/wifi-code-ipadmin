"""Hotspot service cut-off marker for cancelled operators.

isp_operators.hotspot_service_cut_off_at
    When jobs.hotspot_service_end disconnected the operator's clients because
    hotspot_service_ends_at (migration 058) had passed: the job's "done" mark,
    so it acts and writes its audit row once per wind-down, including for an
    operator with nobody online. RADIUS refuses logins from
    hotspot_service_ends_at on its own (freeradius/sql.conf); this column
    only records that the disconnects were sent. Cleared on restore.
    ck_isp_operators_cut_off: only set while there is an end date, so only
    while status = 'cancelled' (058's ck_isp_operators_cancellation).

Downgrade drops the column and the CHECK.

Revision ID: 059_hotspot_service_cut_off
Revises: 058_operator_cancellation
"""
from alembic import op
import sqlalchemy as sa

revision = "059_hotspot_service_cut_off"
down_revision = "058_operator_cancellation"
branch_labels = None
depends_on = None


def upgrade():
    # Fail fast rather than queue behind a long-held lock on isp_operators
    # (and block every sign-in queued behind us). LOCAL: this transaction only.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("isp_operators", sa.Column("hotspot_service_cut_off_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_isp_operators_cut_off",
        "isp_operators",
        "hotspot_service_cut_off_at IS NULL OR hotspot_service_ends_at IS NOT NULL",
    )


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_isp_operators_cut_off", "isp_operators", type_="check")
    op.drop_column("isp_operators", "hotspot_service_cut_off_at")
