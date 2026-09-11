from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import PaymentTransaction, Plan, ResellerVoucherAllocation, Router, Session, Voucher
from src.middleware.auth import TenantContext, get_admin_tenant_context
from src.modules.mikrotik.setup_routes import _is_online as router_is_online

router = APIRouter(prefix="/analytics", tags=["analytics"])

_BYTES_PER_MB = 1024 * 1024


def _period_starts(now: datetime) -> tuple[datetime, datetime, datetime]:
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)
    return today_start, week_start, month_start


@router.get("/snapshot")
async def analytics_snapshot(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    operator_id = tenant.isp_operator_id
    now = datetime.now(timezone.utc)
    today_start, week_start, month_start = _period_starts(now)

    # --- Revenue (today / this week / this month), one round trip via conditional sums.
    revenue_row = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(case((PaymentTransaction.completed_at >= today_start, PaymentTransaction.amount_ghs), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((PaymentTransaction.completed_at >= week_start, PaymentTransaction.amount_ghs), else_=0)),
                    0,
                ),
                func.coalesce(func.sum(PaymentTransaction.amount_ghs), 0),
            ).where(
                PaymentTransaction.isp_operator_id == operator_id,
                PaymentTransaction.status == "success",
                PaymentTransaction.completed_at >= month_start,
            )
        )
    ).one()
    revenue_today, revenue_week, revenue_month = (float(v) for v in revenue_row)

    # --- Active sessions right now.
    active_sessions = (
        await db.execute(
            select(func.count()).select_from(Session).where(
                Session.isp_operator_id == operator_id,
                Session.stopped_at.is_(None),
            )
        )
    ).scalar() or 0

    # --- Vouchers: sold (tracked channels) vs. redeemed vs. unredeemed inventory.
    #
    # "Sold" only has real signal for the two channels that leave a transaction
    # record: a direct portal purchase (payment_transactions) or a reseller sale
    # (reseller_voucher_allocations.sold_at). Vouchers handed out through
    # POST /vouchers/generate — admin-issued batches, printed/handed to a
    # customer at the counter, promo giveaways, test vouchers — leave NO hand-off
    # signal anywhere in the schema (Voucher has no issued_at/distributed_at), so
    # they are structurally invisible to "sold" and reported as a lower bound,
    # not a true count of every voucher actually in customers' hands.
    #
    # Redeemed and unredeemed inventory are therefore channel-agnostic instead of
    # scoped to the "sold" set: activated_at / status already record real usage
    # regardless of how the voucher reached the customer, so those two numbers
    # stay accurate for every distribution path, including admin-issued ones.
    sold_via_payment = select(PaymentTransaction.voucher_id.label("voucher_id")).where(
        PaymentTransaction.isp_operator_id == operator_id,
        PaymentTransaction.status == "success",
        PaymentTransaction.voucher_id.isnot(None),
    )
    sold_via_reseller = (
        select(ResellerVoucherAllocation.voucher_id.label("voucher_id"))
        .join(Voucher, Voucher.id == ResellerVoucherAllocation.voucher_id)
        .where(
            Voucher.isp_operator_id == operator_id,
            ResellerVoucherAllocation.sold_at.isnot(None),
        )
    )
    sold_ids_subq = sold_via_payment.union(sold_via_reseller).subquery()
    sold_count = (await db.execute(select(func.count()).select_from(sold_ids_subq))).scalar() or 0

    redeemed_count = (
        await db.execute(
            select(func.count()).select_from(Voucher).where(
                Voucher.isp_operator_id == operator_id,
                Voucher.activated_at.isnot(None),
            )
        )
    ).scalar() or 0

    unredeemed_inventory = (
        await db.execute(
            select(func.count()).select_from(Voucher).where(
                Voucher.isp_operator_id == operator_id,
                Voucher.status == "unused",
            )
        )
    ).scalar() or 0

    # --- Routers online/offline, same predicate as the platform drill-down.
    routers = (
        await db.execute(select(Router).where(Router.isp_operator_id == operator_id))
    ).scalars().all()
    routers_online = sum(1 for r in routers if router_is_online(r))

    # --- Top package this month, ranked by voucher count (revenue shown alongside).
    payment_sales = select(
        PaymentTransaction.plan_id.label("plan_id"),
        PaymentTransaction.amount_ghs.label("revenue_ghs"),
    ).where(
        PaymentTransaction.isp_operator_id == operator_id,
        PaymentTransaction.status == "success",
        PaymentTransaction.completed_at >= month_start,
    )
    reseller_sales = (
        select(
            Voucher.plan_id.label("plan_id"),
            ResellerVoucherAllocation.purchase_price_ghs.label("revenue_ghs"),
        )
        .join(Voucher, Voucher.id == ResellerVoucherAllocation.voucher_id)
        .where(
            Voucher.isp_operator_id == operator_id,
            ResellerVoucherAllocation.sold_at.isnot(None),
            ResellerVoucherAllocation.sold_at >= month_start,
        )
    )
    sales_union = payment_sales.union_all(reseller_sales).subquery()

    top_row = (
        await db.execute(
            select(
                sales_union.c.plan_id,
                func.count().label("voucher_count"),
                func.coalesce(func.sum(sales_union.c.revenue_ghs), 0).label("revenue_ghs"),
            )
            .group_by(sales_union.c.plan_id)
            .order_by(func.count().desc())
            .limit(1)
        )
    ).first()

    top_package = None
    if top_row is not None:
        # Re-scoped defensively: plan_id came from an operator-scoped sale, but
        # every joined table gets its own isp_operator_id check, not a transitive one.
        plan = (
            await db.execute(
                select(Plan).where(Plan.id == top_row.plan_id, Plan.isp_operator_id == operator_id)
            )
        ).scalar_one_or_none()
        if plan is not None:
            top_package = {
                "plan_id": str(plan.id),
                "name": plan.name,
                "voucher_count": top_row.voucher_count,
                "revenue_ghs": float(top_row.revenue_ghs),
            }

    # --- Data used (today / this week / this month).
    # sessions.upload_bytes/download_bytes are cumulative totals updated in place
    # by FreeRADIUS interim/stop accounting (see freeradius/sql.conf) — there is no
    # per-day traffic breakdown in this schema. Each session's current total is
    # attributed wholly to the bucket containing its started_at, which slightly
    # overcounts sessions that span a period boundary. Acceptable approximation at
    # this platform's voucher durations, but not exact.
    usage_total = Session.upload_bytes + Session.download_bytes
    data_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(case((Session.started_at >= today_start, usage_total), else_=0)), 0),
                func.coalesce(func.sum(case((Session.started_at >= week_start, usage_total), else_=0)), 0),
                func.coalesce(func.sum(usage_total), 0),
            ).where(
                Session.isp_operator_id == operator_id,
                Session.started_at >= month_start,
            )
        )
    ).one()
    data_used_today_mb, data_used_week_mb, data_used_month_mb = (
        round(float(v) / _BYTES_PER_MB, 2) for v in data_row
    )

    return {
        "revenue_ghs": {
            "today": revenue_today,
            "this_week": revenue_week,
            "this_month": revenue_month,
        },
        "active_sessions": active_sessions,
        "vouchers": {
            # Lower bound: only payment_transactions + reseller_voucher_allocations
            # sales are trackable. Admin-issued vouchers (POST /vouchers/generate)
            # leave no hand-off signal and are not counted here — see comment above.
            "sold_tracked_channels": sold_count,
            "redeemed": redeemed_count,
            "unredeemed_inventory": unredeemed_inventory,
        },
        "routers": {
            "online": routers_online,
            "offline": len(routers) - routers_online,
            "total": len(routers),
        },
        "top_package": top_package,
        "data_used_mb": {
            "today": data_used_today_mb,
            "this_week": data_used_week_mb,
            "this_month": data_used_month_mb,
        },
    }


@router.get("/trends")
async def analytics_trends(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    operator_id = tenant.isp_operator_id
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)

    revenue_day = func.date_trunc("day", PaymentTransaction.completed_at)
    revenue_rows = (
        await db.execute(
            select(revenue_day.label("day"), func.sum(PaymentTransaction.amount_ghs).label("amount_ghs"))
            .where(
                PaymentTransaction.isp_operator_id == operator_id,
                PaymentTransaction.status == "success",
                PaymentTransaction.completed_at >= start,
            )
            .group_by(revenue_day)
        )
    ).all()
    revenue_by_day = {row.day.date().isoformat(): float(row.amount_ghs) for row in revenue_rows}

    redemption_day = func.date_trunc("day", Voucher.activated_at)
    redemption_rows = (
        await db.execute(
            select(redemption_day.label("day"), func.count().label("count"))
            .where(
                Voucher.isp_operator_id == operator_id,
                Voucher.activated_at >= start,
            )
            .group_by(redemption_day)
        )
    ).all()
    redemptions_by_day = {row.day.date().isoformat(): row.count for row in redemption_rows}

    days = [(start + timedelta(days=i)).date().isoformat() for i in range(30)]
    return {
        "revenue_by_day": [{"date": d, "amount_ghs": revenue_by_day.get(d, 0.0)} for d in days],
        "redemptions_by_day": [{"date": d, "count": redemptions_by_day.get(d, 0)} for d in days],
    }
