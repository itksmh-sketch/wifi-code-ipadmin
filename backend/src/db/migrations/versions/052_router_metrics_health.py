"""Point-in-time router health snapshot alongside each metrics sample.

``router_metrics`` already records the numeric series the Metrics tab charts
(CPU, memory, sessions, bytes). This adds the *state* half — the things that
are either fine or not, and that no chart makes clearer:

  * disk free/used — already in the ``/system/resource`` response that
    ``collect_router_metrics`` prints every 5 minutes and mostly discards
  * sensors (temperature/voltage) from ``/system/health``, where the board
    has them at all
  * DHCP pool occupancy from ``/ip/pool``, which on RouterOS 7 returns
    total/used/available directly — no lease counting
  * per-interface link-flap history from the ``/interface`` print
    (``link-downs``, ``last-link-down-time``)
  * RouterBOARD firmware current-vs-available, where applicable

A JSONB column on the existing table rather than a new table, because the
snapshot is sampled on exactly the same 5-minute cadence as the row it belongs
to, the per-interface part is variable-shaped, the router-detail endpoint
already loads the latest RouterMetric row (so "as of" costs no extra query),
and it inherits the existing 24h prune in collect_router_metrics for free.

Nullable with no default and no backfill: rows written before the collector
change simply have NULL, which the UI renders as "not collected yet". Nothing
reads this column until that change lands, so applying this migration on its
own is inert.

Shape (see the collector for the writer; ``schema`` is versioned so a later
change is detectable rather than silently mis-rendered):

    {"schema": 1,
     "disk": {"free_bytes": .., "total_bytes": .., "used_percent": ..},
     "sensors": {"supported": bool, "reason": str|null,
                 "temperature_c": float|null, "voltage_v": float|null},
     "pool": [{"name": .., "total": .., "used": .., "available": ..,
               "used_percent": ..}],
     "interfaces": [{"name": .., "running": bool, "disabled": bool,
                     "link_downs": int, "last_link_down": str|null}],
     "firmware": {"supported": bool, "current": str|null, "available": str|null},
     "errors": [str]}

Every RouterOS path is fetched under its own try/except and contributes
``supported: false`` (or an ``errors`` entry) rather than failing the whole
snapshot — ``/system/routerboard`` hard-errors with "no such command prefix"
on CHR/x86, and ``/system/health`` returns no sensor keys on boards without
sensors, both of which are normal for this fleet.

Revision ID: 052_router_metrics_health
Revises: 051_backfill_onboarding_checklist
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "052_router_metrics_health"
down_revision = "051_backfill_onboarding_checklist"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "router_metrics",
        sa.Column("health", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade():
    op.drop_column("router_metrics", "health")
