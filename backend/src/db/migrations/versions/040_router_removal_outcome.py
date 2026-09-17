"""Record the outcome of router removal durably on the router row.

Before this, the only record of whether an operator's "Remove router" action
actually confirmed disconnecting already-online customers was a one-time HTTP
response and a log line — nothing the router's own page could show reliably
later. For the case that matters most (the router was unreachable at removal
time, so we could not confirm anyone was kicked off), that meant no durable,
visible signal distinguishing "fully disconnected" from "may still be
online" — exactly backwards for a theft/compromise scenario.

All four columns are nullable, set once inside remove_router() at removal
time, and never touched again (no reactivation flow exists to reset them).
Purely additive: no backfill, no change to any existing column.

Revision ID: 040_router_removal_outcome
Revises: 039_platform_notification_sms_credentials
"""
from alembic import op
import sqlalchemy as sa

revision = "040_router_removal_outcome"
down_revision = "039_platform_notification_sms_credentials"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("routers", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("routers", sa.Column("removal_router_reachable", sa.Boolean(), nullable=True))
    op.add_column("routers", sa.Column("removal_sessions_disconnected", sa.Integer(), nullable=True))
    op.add_column("routers", sa.Column("removal_sessions_failed", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("routers", "removal_sessions_failed")
    op.drop_column("routers", "removal_sessions_disconnected")
    op.drop_column("routers", "removal_router_reachable")
    op.drop_column("routers", "removed_at")
