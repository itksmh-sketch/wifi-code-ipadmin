"""Remove the global session identity (phase B).

During the rollout this file was held OUTSIDE the alembic chain (in
migrations/pending/) so that `alembic upgrade head` — which the backend
container runs automatically on start — could not apply it early. It was moved
here by scripts/deploy_session_id_fix.sh only after both FreeRADIUS instances
had been restarted onto the new config, and applied 2026-09-19.

The reason for that dance, for anyone sequencing a similar change: applying
this while the old sql.conf is still loaded breaks all accounting:
  * "ON CONFLICT (session_id)" errors with "no unique or exclusion constraint
    matching the ON CONFLICT specification" on every Accounting-Start;
  * update_voucher_usage(text) no longer exists, so interim and stop error.

"""
from alembic import op


revision = "046_drop_global_session_id_key"
down_revision = "045_session_router_scoped_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("sessions_session_id_key", "sessions", type_="unique")
    op.execute("DROP FUNCTION IF EXISTS update_voucher_usage(text)")


def downgrade() -> None:
    # Not cleanly reversible: once router-scoped duplicates exist, restoring a
    # global UNIQUE(session_id) would fail, and "fixing" that means deleting
    # real session rows. Restore from backup instead.
    raise RuntimeError(
        "046 is not reversible — a global UNIQUE(session_id) cannot be restored "
        "once router-scoped duplicate session_ids exist. Restore from backup."
    )
