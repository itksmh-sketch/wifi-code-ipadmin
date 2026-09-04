"""Per-tenant duplicate-plan detection.

Two plans are duplicates when they belong to the same operator, are scoped to the
same site, and carry identical settings: type, duration, data cap, both speed
limits and price. Name is deliberately NOT part of the comparison -- renaming a
package does not make it a different package.

`site_id`, `duration_minutes` and `data_limit_mb` are nullable (a global plan has
site_id NULL, an unlimited-data plan has data_limit_mb NULL), so they are compared
with IS NOT DISTINCT FROM. Plain `=` evaluates NULL = NULL to NULL rather than
true, which would let two otherwise identical unlimited plans slip through as
non-duplicates.

Dedup is per-tenant by construction: every query is anchored on isp_operator_id,
so operators A and B may each hold the same plan.
"""
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from src.db.models import Plan

DUPLICATE_PLAN_MESSAGE = "A plan with these settings already exists"


def normalize_price(price_ghs) -> Decimal:
    """Round to the stored Numeric(10, 2) scale so 2.0 and 2.00 compare equal and
    a float such as 2.005 is rounded the way Postgres rounds it on insert -- a
    mismatch here would report "no duplicate" and then store one."""
    return Decimal(str(price_ghs)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def duplicate_plan_filters(
    isp_operator_id: uuid.UUID,
    *,
    site_id: Optional[uuid.UUID],
    plan_type: str,
    duration_minutes: Optional[int],
    data_limit_mb: Optional[int],
    download_speed_kbps: int,
    upload_speed_kbps: int,
    price_ghs,
) -> list[ColumnElement[bool]]:
    """Conditions matching an existing plan with the same tenant, site and settings."""
    return [
        Plan.isp_operator_id == isp_operator_id,
        Plan.site_id.is_not_distinct_from(site_id),
        Plan.type == plan_type,
        Plan.duration_minutes.is_not_distinct_from(duration_minutes),
        Plan.data_limit_mb.is_not_distinct_from(data_limit_mb),
        Plan.download_speed_kbps == download_speed_kbps,
        Plan.upload_speed_kbps == upload_speed_kbps,
        Plan.price_ghs == normalize_price(price_ghs),
    ]


async def find_duplicate_plan(
    db: AsyncSession,
    isp_operator_id: uuid.UUID,
    *,
    exclude_plan_id: Optional[uuid.UUID] = None,
    **settings,
) -> Optional[Plan]:
    """Return an existing plan of this operator with identical settings, if any.

    `exclude_plan_id` skips the row being edited so an update that leaves the
    settings untouched does not flag the plan as its own duplicate.
    """
    filters = duplicate_plan_filters(isp_operator_id, **settings)
    if exclude_plan_id is not None:
        filters.append(Plan.id != exclude_plan_id)
    result = await db.execute(select(Plan).where(*filters).limit(1))
    return result.scalars().first()


# The PlanUpdate fields that feed the comparison above -- an edit touching none of
# them (a rename, an is_active toggle) cannot create a duplicate.
SETTINGS_FIELDS = frozenset(
    {"type", "duration_minutes", "data_limit_mb", "download_speed_kbps", "upload_speed_kbps", "price_ghs"}
)
