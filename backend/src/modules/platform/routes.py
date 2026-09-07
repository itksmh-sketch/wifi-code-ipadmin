from datetime import datetime, timedelta, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from decimal import Decimal
from typing import Optional
from src.db.models import (
    AdminUser,
    ISPOperator,
    OperatorBillingEvent,
    OperatorInvoice,
    OperatorPaymentCredential,
    PaymentTransaction,
    PlatformOwner,
    PlatformPaymentCredential,
    Plan,
    ProviderCatalogEntry,
    Router,
    RouterCredential,
    RouterSetupStatus,
    Session,
    Site,
    Town,
    Voucher,
)
from src.middleware.auth import get_platform_owner_context
from src.modules.mikrotik import setup_status as setup_store
# Reuse the admin view's reachability predicate verbatim so the platform
# drill-down can never disagree with what an operator's own dashboard shows.
from src.modules.mikrotik.setup_routes import _is_online as router_is_online
from src.middleware.rate_limit import enforce_rate_limit
from src.schemas import LoginRequest, PlatformAdminCreate, PlatformOperatorBillingUpdate, PlatformOperatorCreate, PlatformOperatorStatusUpdate, PlatformPaymentCredentialResponse, PlatformPaymentCredentialUpdate, ProviderCatalogEntryResponse, ProviderCatalogUpdate, RefreshRequest, TokenResponse
from src.utils.encryption import encrypt_secret
from src.utils.auth import (
    create_platform_owner_access_token,
    create_platform_owner_refresh_token,
    hash_password,
    verify_password,
    verify_platform_owner_token,
)

router = APIRouter(prefix="/platform", tags=["platform"])

VALID_OPERATOR_STATUSES = {"pending", "approved", "suspended", "cancelled"}


def _month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@router.post("/auth/login", response_model=TokenResponse)
async def platform_auth_login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "platform:login", limit=10, window_seconds=60)
    result = await db.execute(select(PlatformOwner).where(PlatformOwner.email == body.email, PlatformOwner.is_active == True))
    owner = result.scalar_one_or_none()
    if not owner or not verify_password(body.password, owner.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    owner.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    token_data = {"sub": str(owner.id), "role": "platform_owner", "email": owner.email}
    return TokenResponse(
        access_token=create_platform_owner_access_token(token_data),
        refresh_token=create_platform_owner_refresh_token(token_data),
    )


@router.post("/auth/refresh", response_model=TokenResponse)
async def platform_auth_refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    payload = verify_platform_owner_token(body.refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    owner_id = payload.get("sub")
    result = await db.execute(select(PlatformOwner).where(PlatformOwner.id == owner_id, PlatformOwner.is_active == True))
    owner = result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Platform owner not found or inactive")
    token_data = {"sub": str(owner.id), "role": "platform_owner", "email": owner.email}
    return TokenResponse(
        access_token=create_platform_owner_access_token(token_data),
        refresh_token=create_platform_owner_refresh_token(token_data),
    )


async def _operator_row(db: AsyncSession, operator: ISPOperator) -> dict:
    start = _month_start()
    admin_count = (
        await db.execute(
            select(func.count()).select_from(AdminUser).where(AdminUser.isp_operator_id == operator.id)
        )
    ).scalar() or 0
    voucher_count = (
        await db.execute(
            select(func.count()).select_from(Voucher).where(
                Voucher.isp_operator_id == operator.id,
                Voucher.created_at >= start,
            )
        )
    ).scalar() or 0
    monthly_revenue = (
        await db.execute(
            select(func.coalesce(func.sum(PaymentTransaction.amount_ghs), 0)).where(
                PaymentTransaction.isp_operator_id == operator.id,
                PaymentTransaction.status == "success",
                PaymentTransaction.completed_at >= start,
            )
        )
    ).scalar() or 0
    return {
        "id": str(operator.id),
        "name": operator.name,
        "slug": operator.slug,
        "contact_email": operator.contact_email,
        "contact_phone": operator.contact_phone,
        "status": operator.status,
        "billing_status": operator.billing_status,
        "created_at": operator.created_at,
        "admin_count": admin_count,
        "voucher_count_this_month": voucher_count,
        "monthly_revenue_this_month": float(monthly_revenue),
    }


@router.get("/me")
async def platform_me(owner: PlatformOwner = Depends(get_platform_owner_context)):
    return {
        "id": str(owner.id),
        "email": owner.email,
        "name": owner.name,
        "last_login_at": owner.last_login_at,
    }


@router.get("/operators")
async def list_operators(
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    result = await db.execute(select(ISPOperator).order_by(ISPOperator.created_at.desc()))
    return [await _operator_row(db, operator) for operator in result.scalars().all()]


@router.post("/operators", status_code=status.HTTP_201_CREATED)
async def create_operator(
    body: PlatformOperatorCreate,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    existing = (
        await db.execute(
            select(ISPOperator).where(
                or_(ISPOperator.slug == body.slug, ISPOperator.contact_email == str(body.contact_email))
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Operator slug or contact email already exists")

    existing_admin = (
        await db.execute(select(AdminUser).where(AdminUser.email == body.initial_admin_email))
    ).scalar_one_or_none()
    if existing_admin:
        raise HTTPException(status_code=409, detail="Initial admin email already exists")

    now = datetime.now(timezone.utc)
    on_trial = body.trial_days is not None
    operator = ISPOperator(
        name=body.name,
        slug=body.slug,
        contact_email=str(body.contact_email),
        contact_phone=body.contact_phone,
        status="approved",
        approved_at=now,
        approved_by_platform_owner_id=owner.id,
        monthly_fee_ghs=body.monthly_fee_ghs,
        billing_status="trial" if on_trial else "active",
        trial_ends_at=(now + timedelta(days=body.trial_days)) if on_trial else None,
    )
    db.add(operator)
    await db.flush()

    if on_trial:
        db.add(
            OperatorBillingEvent(
                isp_operator_id=operator.id,
                event_type="trial_started",
                description=f"Trial started for {operator.name}. Ends {operator.trial_ends_at.date()}.",
                event_metadata={"trial_days": body.trial_days, "monthly_fee_ghs": str(body.monthly_fee_ghs)},
            )
        )

    admin = AdminUser(
        isp_operator_id=operator.id,
        email=str(body.initial_admin_email),
        password_hash=hash_password(body.initial_admin_password),
        role="superadmin",
        is_active=True,
    )
    db.add(admin)
    await db.commit()
    await db.refresh(operator)
    return await _operator_row(db, operator)


@router.get("/operators/{operator_id}")
async def get_operator(
    operator_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    operator = await db.get(ISPOperator, operator_id)
    if not operator:
        raise HTTPException(status_code=404, detail="Operator not found")
    row = await _operator_row(db, operator)
    creds = (
        await db.execute(
            select(OperatorPaymentCredential).where(OperatorPaymentCredential.isp_operator_id == operator.id)
        )
    ).scalar_one_or_none()
    row["payment_credentials"] = {
        "configured": bool(creds),
        "active": bool(creds and creds.is_active),
        "last_validated_at": creds.last_validated_at if creds else None,
        "last_validation_error": creds.last_validation_error if creds else None,
    }
    # The detail page renders total_sessions; without this it only ever came from
    # /summary, so the page showed 0. Kept out of _operator_row so the list
    # endpoint doesn't pick up another per-operator query.
    row["total_sessions"] = (
        await db.execute(select(func.count()).select_from(Session).where(Session.isp_operator_id == operator.id))
    ).scalar() or 0
    return row


@router.put("/operators/{operator_id}/status")
async def update_operator_status(
    operator_id: uuid.UUID,
    body: PlatformOperatorStatusUpdate,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    if body.status not in VALID_OPERATOR_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid operator status")
    operator = await db.get(ISPOperator, operator_id)
    if not operator:
        raise HTTPException(status_code=404, detail="Operator not found")

    previous_status = operator.status
    operator.status = body.status
    operator.updated_at = datetime.now(timezone.utc)
    if body.status == "approved" and not operator.approved_at:
        operator.approved_at = datetime.now(timezone.utc)
        operator.approved_by_platform_owner_id = owner.id
    if body.status == "cancelled":
        operator.billing_status = "cancelled"

    # A suspension the platform owner imposed is NOT reversible by paying an
    # invoice. Tagging it is what stops the billing webhook from lifting it.
    if body.status == "suspended":
        operator.suspension_reason = "manual"
    elif previous_status == "suspended":
        operator.suspension_reason = None

    # Manual status changes previously left no trace at all, so the audit trail
    # covered only billing-driven suspensions — half the story, and the half you
    # least need when explaining why an operator's status changed.
    if body.status == "suspended" and previous_status != "suspended":
        db.add(OperatorBillingEvent(
            isp_operator_id=operator.id,
            event_type="suspended",
            description=f"{operator.name} suspended by platform owner.",
            event_metadata={"reason": "manual", "suspended_by_platform_owner_id": str(owner.id)},
        ))
    elif previous_status == "suspended" and body.status != "suspended":
        db.add(OperatorBillingEvent(
            isp_operator_id=operator.id,
            event_type="reactivated",
            description=f"{operator.name} un-suspended by platform owner (status set to {body.status}).",
            event_metadata={"reason": "manual", "reactivated_by_platform_owner_id": str(owner.id)},
        ))

    await db.commit()
    await db.refresh(operator)
    return await _operator_row(db, operator)


@router.get("/operators/{operator_id}/admins")
async def list_operator_admins(
    operator_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    if not await db.get(ISPOperator, operator_id):
        raise HTTPException(status_code=404, detail="Operator not found")
    result = await db.execute(
        select(AdminUser).where(AdminUser.isp_operator_id == operator_id).order_by(AdminUser.created_at.desc())
    )
    return [
        {
            "id": str(admin.id),
            "email": admin.email,
            "role": admin.role,
            "is_active": bool(admin.is_active),
            "created_at": admin.created_at,
            "last_login_at": admin.last_login_at,
        }
        for admin in result.scalars().all()
    ]


@router.post("/operators/{operator_id}/admins", status_code=status.HTTP_201_CREATED)
async def create_operator_admin(
    operator_id: uuid.UUID,
    body: PlatformAdminCreate,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    if not await db.get(ISPOperator, operator_id):
        raise HTTPException(status_code=404, detail="Operator not found")
    existing = (await db.execute(select(AdminUser).where(AdminUser.email == body.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Admin email already exists")
    admin = AdminUser(
        isp_operator_id=operator_id,
        email=str(body.email),
        password_hash=hash_password(body.password),
        role=body.role.value,
        is_active=True,
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    return {
        "id": str(admin.id),
        "email": admin.email,
        "role": admin.role,
        "is_active": bool(admin.is_active),
        "created_at": admin.created_at,
        "last_login_at": admin.last_login_at,
    }


@router.get("/operators/{operator_id}/summary")
async def operator_summary(
    operator_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    operator = await db.get(ISPOperator, operator_id)
    if not operator:
        raise HTTPException(status_code=404, detail="Operator not found")
    row = await _operator_row(db, operator)
    total_sessions = (
        await db.execute(select(func.count()).select_from(Session).where(Session.isp_operator_id == operator.id))
    ).scalar() or 0
    row["total_sessions"] = total_sessions
    return row


# ---------------------------------------------------------------------------
# Operator infrastructure drill-down (read-only)
# ---------------------------------------------------------------------------
# Platform-owner scope only, and deliberately cross-tenant: the owner sees every
# operator's estate. Strictly a projection — no writes, no router contact, and
# no field of the Router model is ever serialized wholesale (nas_secret /
# nas_secret_plain must never leave the API), so every payload below is built
# key-by-key from an explicit allow-list.


def _ip_str(value) -> Optional[str]:
    """INET columns come back as str or ipaddress objects depending on driver."""
    return str(value) if value is not None else None


def _router_payload(router: Router, setup: Optional[RouterSetupStatus], cred: Optional[RouterCredential]) -> dict:
    tunnel_ip = _ip_str(router.wg_tunnel_ip)
    label_ip = _ip_str(router.ip_address)
    if router.wg_enabled and tunnel_ip:
        connectivity = "tunnel"
    elif label_ip:
        connectivity = "direct"
    else:
        connectivity = "none"
    sections = {
        name: {
            "status": getattr(setup, f"{name}_status", None) or "unconfigured",
            "applied_at": getattr(setup, f"{name}_applied_at", None),
        }
        for name in setup_store.SECTIONS
    }
    sections_complete = setup_store.sections_complete(setup)
    return {
        "id": str(router.id),
        "name": router.name,
        "nas_identifier": router.nas_identifier,
        "is_active": bool(router.is_active),
        # `online` is the single source of truth for the UI badge; the two raw
        # flags are exposed alongside it so the owner can see *why*.
        "online": router_is_online(router),
        "is_online": bool(router.is_online),
        "last_seen_at": router.last_seen_at,
        "connectivity": connectivity,
        "wg_enabled": bool(router.wg_enabled),
        "wg_is_connected": bool(router.wg_is_connected),
        "wg_tunnel_ip": tunnel_ip,
        "wg_last_handshake_at": router.wg_last_handshake_at,
        "ip_address": label_ip,
        "setup": {
            # No setup_status row yet == never provisioned, not an error state.
            "tracked": setup is not None,
            "sections_complete": sections_complete,
            "sections_total": len(setup_store.SECTIONS),
            "provisioned": sections_complete == len(setup_store.SECTIONS),
            **sections,
        },
        "api_credentials": {
            "configured": cred is not None,
            "connection_status": cred.connection_status if cred else "unknown",
            "last_connected_at": cred.last_connected_at if cred else None,
        },
    }


def _plan_payload(plan: Plan, site_name: Optional[str]) -> dict:
    return {
        "id": str(plan.id),
        "name": plan.name,
        "type": plan.type,
        "duration_minutes": plan.duration_minutes,
        "data_limit_mb": plan.data_limit_mb,
        "download_speed_kbps": plan.download_speed_kbps,
        "upload_speed_kbps": plan.upload_speed_kbps,
        "price_ghs": float(plan.price_ghs or 0),
        "is_active": bool(plan.is_active),
        "site_id": str(plan.site_id) if plan.site_id else None,
        # plans.site_id is nullable — a null means the plan is offered estate-wide.
        "site_name": site_name,
        "scope": "site" if plan.site_id else "operator-wide",
        "created_at": plan.created_at,
    }


@router.get("/operators/{operator_id}/infrastructure")
async def operator_infrastructure(
    operator_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    """Towns -> sites -> routers, plus the operator's plans, in 4 flat queries.

    The tree is grouped in memory rather than walked with per-parent queries, so
    the query count is constant no matter how many towns/sites/routers exist.
    """
    operator = await db.get(ISPOperator, operator_id)
    if not operator:
        raise HTTPException(status_code=404, detail="Operator not found")

    # 1) towns
    towns = (
        await db.execute(
            select(Town).where(Town.isp_operator_id == operator_id).order_by(Town.name)
        )
    ).scalars().all()

    # 2) sites
    sites = (
        await db.execute(
            select(Site).where(Site.isp_operator_id == operator_id).order_by(Site.name)
        )
    ).scalars().all()

    # 3) routers + their setup/credential side-tables in one pass (outer joins so
    #    a router that was added but never provisioned still comes back).
    router_rows = (
        await db.execute(
            select(Router, RouterSetupStatus, RouterCredential)
            .outerjoin(RouterSetupStatus, RouterSetupStatus.router_id == Router.id)
            .outerjoin(RouterCredential, RouterCredential.router_id == Router.id)
            .where(Router.isp_operator_id == operator_id)
            .order_by(Router.name)
        )
    ).all()

    # 4) plans + the site label for site-scoped ones
    plan_rows = (
        await db.execute(
            select(Plan, Site.name)
            .outerjoin(Site, Site.id == Plan.site_id)
            .where(Plan.isp_operator_id == operator_id)
            .order_by(Plan.price_ghs, Plan.name)
        )
    ).all()

    routers_by_site: dict[str, list] = {}
    routers_online = 0
    for router_row, setup_row, cred_row in router_rows:
        payload = _router_payload(router_row, setup_row, cred_row)
        if payload["online"]:
            routers_online += 1
        routers_by_site.setdefault(str(router_row.site_id), []).append(payload)

    sites_by_town: dict[str, list] = {}
    orphan_sites: list = []
    town_ids = {str(town.id) for town in towns}
    for site in sites:
        site_payload = {
            "id": str(site.id),
            "name": site.name,
            "address": site.address,
            "created_at": site.created_at,
            "routers": routers_by_site.pop(str(site.id), []),
        }
        town_key = str(site.town_id) if site.town_id else None
        if town_key in town_ids:
            sites_by_town.setdefault(town_key, []).append(site_payload)
        else:
            # Town missing/mismatched — surface the site rather than dropping it.
            orphan_sites.append(site_payload)

    town_payloads = []
    for town in towns:
        town_sites = sites_by_town.get(str(town.id), [])
        town_payloads.append(
            {
                "id": str(town.id),
                "name": town.name,
                "region": town.region,
                "created_at": town.created_at,
                "site_count": len(town_sites),
                "router_count": sum(len(s["routers"]) for s in town_sites),
                "sites": town_sites,
            }
        )

    # Anything left in routers_by_site points at a site this operator doesn't own.
    orphan_routers = [r for group in routers_by_site.values() for r in group]

    plans = [_plan_payload(plan, site_name) for plan, site_name in plan_rows]

    return {
        "operator": {
            "id": str(operator.id),
            "name": operator.name,
            "slug": operator.slug,
            "status": operator.status,
        },
        "towns": town_payloads,
        "unassigned_sites": orphan_sites,
        "unassigned_routers": orphan_routers,
        "plans": plans,
        "totals": {
            "towns": len(town_payloads),
            "sites": len(sites),
            "routers": len(router_rows),
            "routers_online": routers_online,
            "routers_offline": len(router_rows) - routers_online,
            "plans": len(plans),
            "plans_active": sum(1 for p in plans if p["is_active"]),
        },
    }


# ---------------------------------------------------------------------------
# Platform billing endpoints
#
# These have no UI at present. Their only consumer was the React platform page
# frontend/src/pages/platform/PlatformBilling.jsx, which read /billing/summary
# and /billing/operators into a summary + per-operator table (its waive button
# was never implemented — it alerted "use the API directly"). That page was
# deleted when the React platform-owner portal was retired in favour of the
# vanilla portal at /platform/*; see git history for the markup it rendered.
#
# /billing/summary, /billing/operators, /operators/{id}/billing and
# /invoices/{id}/waive are deliberately kept: they are the foundation for the
# vanilla platform-billing page (feature #3), which is where the retired
# React page's display should be rebuilt — this time with a working waive.
# ---------------------------------------------------------------------------

@router.get("/billing/summary")
async def platform_billing_summary(
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    month_start = _month_start()
    total_active = (
        await db.execute(select(func.count()).select_from(ISPOperator).where(ISPOperator.status == "approved"))
    ).scalar() or 0
    on_trial = (
        await db.execute(select(func.count()).select_from(ISPOperator).where(ISPOperator.billing_status == "trial"))
    ).scalar() or 0
    overdue = (
        await db.execute(
            select(func.count()).select_from(ISPOperator).where(ISPOperator.billing_status == "past_due")
        )
    ).scalar() or 0
    mrr = (
        await db.execute(
            select(func.coalesce(func.sum(ISPOperator.monthly_fee_ghs), 0)).where(
                ISPOperator.billing_status == "active"
            )
        )
    ).scalar() or Decimal("0")
    collected = (
        await db.execute(
            select(func.coalesce(func.sum(OperatorInvoice.amount_ghs), 0)).where(
                OperatorInvoice.status == "paid",
                OperatorInvoice.paid_at >= month_start,
            )
        )
    ).scalar() or Decimal("0")
    return {
        "total_active_operators": total_active,
        "operators_on_trial": on_trial,
        "operators_overdue": overdue,
        "monthly_recurring_revenue_ghs": float(mrr),
        "revenue_collected_this_month_ghs": float(collected),
    }


@router.get("/billing/operators")
async def platform_billing_operators(
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    operators = (await db.execute(select(ISPOperator).order_by(ISPOperator.name))).scalars().all()

    # An operator can carry several unpaid invoices at once, so these are
    # aggregated rather than fetched as a single row — reading one invoice with
    # scalar_one_or_none() raised MultipleResultsFound and 500'd the endpoint.
    outstanding_rows = (
        await db.execute(
            select(
                OperatorInvoice.isp_operator_id,
                func.count().label("count"),
                func.coalesce(func.sum(OperatorInvoice.amount_ghs), 0).label("total"),
                func.min(OperatorInvoice.due_at).label("next_due_at"),
            )
            .where(OperatorInvoice.status.in_(["issued", "overdue"]))
            .group_by(OperatorInvoice.isp_operator_id)
        )
    ).all()
    outstanding_by_operator = {row.isp_operator_id: row for row in outstanding_rows}

    # The oldest unpaid invoice is the one the operator is chased for, so it is
    # the one worth naming on the row.
    oldest_rows = (
        await db.execute(
            select(OperatorInvoice)
            .where(OperatorInvoice.status.in_(["issued", "overdue"]))
            .order_by(OperatorInvoice.isp_operator_id, OperatorInvoice.created_at.asc())
        )
    ).scalars().all()
    oldest_by_operator: dict = {}
    for invoice in oldest_rows:
        oldest_by_operator.setdefault(invoice.isp_operator_id, invoice)

    last_paid_rows = (
        await db.execute(
            select(
                OperatorInvoice.isp_operator_id,
                func.max(OperatorInvoice.paid_at).label("last_paid_at"),
            )
            .where(OperatorInvoice.status == "paid")
            .group_by(OperatorInvoice.isp_operator_id)
        )
    ).all()
    last_paid_by_operator = {row.isp_operator_id: row.last_paid_at for row in last_paid_rows}

    rows = []
    for op in operators:
        outstanding = outstanding_by_operator.get(op.id)
        oldest = oldest_by_operator.get(op.id)
        last_paid_at = last_paid_by_operator.get(op.id)
        rows.append({
            "id": str(op.id),
            "name": op.name,
            "slug": op.slug,
            "billing_status": op.billing_status,
            "monthly_fee_ghs": float(op.monthly_fee_ghs),
            "trial_ends_at": op.trial_ends_at.isoformat() if op.trial_ends_at else None,
            "last_paid_at": last_paid_at.isoformat() if last_paid_at else None,
            "next_due_at": outstanding.next_due_at.isoformat() if outstanding and outstanding.next_due_at else None,
            # Total across every unpaid invoice, not just the oldest one.
            "outstanding_amount_ghs": float(outstanding.total) if outstanding else 0,
            "outstanding_invoice_count": int(outstanding.count) if outstanding else 0,
            "outstanding_invoice_number": oldest.invoice_number if oldest else None,
        })
    return rows


@router.put("/operators/{operator_id}/billing")
async def update_operator_billing(
    operator_id: uuid.UUID,
    body: PlatformOperatorBillingUpdate,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    """Set an operator's monthly fee and/or extend their trial."""
    operator = await db.get(ISPOperator, operator_id)
    if not operator:
        raise HTTPException(404, "Operator not found")
    if body.monthly_fee_ghs is None and body.extend_trial_days is None:
        raise HTTPException(400, "Provide monthly_fee_ghs and/or extend_trial_days")

    if body.monthly_fee_ghs is not None:
        operator.monthly_fee_ghs = body.monthly_fee_ghs
    if body.extend_trial_days is not None:
        # Previously a silent no-op when the operator had no trial to extend.
        if not operator.trial_ends_at:
            raise HTTPException(400, f"{operator.name} is not on a trial, so there is nothing to extend")
        operator.trial_ends_at = operator.trial_ends_at + timedelta(days=body.extend_trial_days)

    await db.commit()
    await db.refresh(operator)
    return {
        "message": "Updated",
        "operator_id": str(operator.id),
        "monthly_fee_ghs": float(operator.monthly_fee_ghs),
        "trial_ends_at": operator.trial_ends_at.isoformat() if operator.trial_ends_at else None,
    }


@router.put("/invoices/{invoice_id}/waive")
async def waive_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    invoice = await db.get(OperatorInvoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    if invoice.status == "paid":
        raise HTTPException(400, "Cannot waive a paid invoice")
    invoice.status = "waived"
    event = OperatorBillingEvent(
        isp_operator_id=invoice.isp_operator_id,
        event_type="waived",
        description=f"Invoice {invoice.invoice_number} waived by platform owner.",
        event_metadata={"invoice_number": invoice.invoice_number, "waived_by": str(owner.id)},
    )
    db.add(event)
    await db.commit()
    return {"message": "Invoice waived", "invoice_id": str(invoice.id)}


# --- Platform settings (platform owner only) ---

@router.get("/settings")
async def get_platform_settings(
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Return all platform-level settings as a flat key/value object.

    Keys: wg_server_endpoint, platform_app_url, webhook_base_url. Values come
    from the platform_settings table, falling back to the .env/config default.
    """
    from src.modules.platform.settings_service import get_all_settings
    return await get_all_settings(db)


@router.put("/settings")
async def update_platform_settings(
    updates: dict[str, str],
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Update one or more platform settings. Only known safe keys are allowed."""
    from src.modules.platform.settings_service import PLATFORM_SETTING_KEYS, get_all_settings, set_setting

    unknown = set(updates) - set(PLATFORM_SETTING_KEYS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown setting key(s): {', '.join(sorted(unknown))}",
        )
    for key, value in updates.items():
        await set_setting(db, key, str(value if value is not None else ""))
    await db.commit()
    return await get_all_settings(db)


# --- Service health monitor (platform owner only, strictly read-only) ---

@router.get("/health/services")
async def platform_service_health(
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    """Per-service up/down/degraded status for the platform health monitor.

    Read-only by design: it probes reachability and app-level health endpoints
    the backend can already reach, and exposes no lifecycle controls. Restarting
    a service is Docker-daemon territory and is deliberately out of scope.

    Each probe opens its own connection, so a hung dependency can never
    invalidate the session this request is authenticated on.
    """
    from src.modules.platform.health_service import collect_service_health

    return await collect_service_health()


# --- Provider catalog (platform owner only) ---

def _catalog_row(entry: ProviderCatalogEntry) -> dict:
    return {
        "id": str(entry.id),
        "category": entry.category,
        "provider_key": entry.provider_key,
        "display_name": entry.display_name,
        "description": entry.description,
        "credential_schema": entry.credential_schema or {},
        "is_integrated": bool(entry.is_integrated),
        "is_available": bool(entry.is_available),
        "is_platform_provided": bool(entry.is_platform_provided),
        # String, not float: the rate is Numeric(10,4) and money must not go
        # through a binary float on the way to the browser.
        "platform_rate_per_message": (
            str(entry.platform_rate_per_message) if entry.platform_rate_per_message is not None else None
        ),
        "sort_order": entry.sort_order,
    }


@router.get("/providers", response_model=dict[str, list[ProviderCatalogEntryResponse]])
async def list_provider_catalog(
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """The full provider catalog, grouped by category.

    Rows are read-only apart from the availability toggle and the per-message
    rate; there is deliberately no create or delete endpoint, since the catalog
    is defined by the migration/seed.
    """
    result = await db.execute(
        select(ProviderCatalogEntry).order_by(
            ProviderCatalogEntry.category,
            ProviderCatalogEntry.sort_order,
            ProviderCatalogEntry.display_name,
        )
    )
    entries = result.scalars().all()
    grouped: dict[str, list] = {"payment": [], "sms": []}
    for entry in entries:
        grouped.setdefault(entry.category, []).append(_catalog_row(entry))
    return grouped


@router.put("/providers/{entry_id}", response_model=ProviderCatalogEntryResponse)
async def update_provider_catalog_entry(
    entry_id: uuid.UUID,
    body: ProviderCatalogUpdate,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Toggle availability and/or set the platform per-message rate.

    Enabling a provider whose integration does not exist yet is a 409, not a
    silently-ignored write — the UI disables the control, but the server is what
    actually guarantees operators are never offered a dead provider.
    """
    entry = (
        await db.execute(select(ProviderCatalogEntry).where(ProviderCatalogEntry.id == entry_id))
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    if body.is_available is not None:
        if body.is_available and not entry.is_integrated:
            raise HTTPException(
                status_code=409,
                detail=f"{entry.display_name} is not yet integrated and cannot be made available to operators.",
            )
        entry.is_available = body.is_available

    if body.clear_platform_rate:
        entry.platform_rate_per_message = None
    elif body.platform_rate_per_message is not None:
        if not entry.is_platform_provided:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{entry.display_name} is not platform-provided; operators are billed by the "
                    "provider directly, so there is no platform rate to set."
                ),
            )
        entry.platform_rate_per_message = body.platform_rate_per_message

    entry.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(entry)
    return _catalog_row(entry)


# --- Platform payment credentials (platform owner only) ---
#
# The platform's own keys — how operator subscriptions are collected. Distinct
# from /payment-credentials, which is an operator's keys for selling vouchers.
#
# Storage and UI only, for now: initiate_invoice_payment and the platform-billing
# webhook still read settings directly. Switching them to this table is Phase 4,
# deliberately after the webhook gains amount/currency verification in Phase 3.

def _last4(value: str | None) -> str | None:
    return value[-4:] if value else None


def _validate_paystack_keys(public_key: str, secret_key: str) -> None:
    if not (public_key.startswith("pk_test_") or public_key.startswith("pk_live_")):
        raise HTTPException(status_code=400, detail="Paystack public key must start with pk_test_ or pk_live_")
    if not (secret_key.startswith("sk_test_") or secret_key.startswith("sk_live_")):
        raise HTTPException(status_code=400, detail="Paystack secret key must start with sk_test_ or sk_live_")



async def _credential_response(db: AsyncSession) -> PlatformPaymentCredentialResponse:
    """Masked view of whichever keys are actually in force.

    Only the last 4 characters ever leave this function — the raw values stay in
    the resolver.
    """
    from src.modules.platform import payment_credentials_service as creds_service

    # What is actually in force — the active row, or .env when there is none.
    keys = await creds_service.resolve_paystack_keys(db)
    # The stored row, if any, whether or not it is the one in force. A
    # deactivated row still needs reporting: "stored but switched off" is a
    # legitimate state (mid provider-switch, say), not an absence.
    row = await creds_service.get_credential(db)
    shown = creds_service.keys_from_credential(row) if row is not None else keys
    return PlatformPaymentCredentialResponse(
        provider=row.provider if row else "paystack",
        public_key_last4=_last4(shown.public_key),
        secret_key_last4=_last4(shown.secret_key),
        webhook_secret_last4=_last4(shown.webhook_secret),
        is_stored=row is not None,
        stored_updated_at=row.updated_at if row else None,
        is_active=bool(row.is_active) if row else False,
        is_configured=keys.is_configured,
        source=keys.source,
        last_validated_at=row.last_validated_at if row else None,
        last_validation_error=row.last_validation_error if row else None,
    )


@router.get("/payment-credentials", response_model=PlatformPaymentCredentialResponse)
async def get_platform_payment_credentials(
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Masked view of the platform's payment keys.

    `source` says whether the values come from the table ("db") or the
    PLATFORM_BILLING_PAYSTACK_* env vars ("env"), which is the read-through
    fallback while the table has no active row.
    """
    return await _credential_response(db)


@router.put("/payment-credentials", response_model=PlatformPaymentCredentialResponse)
async def update_platform_payment_credentials(
    body: PlatformPaymentCredentialUpdate,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Store the platform's payment keys, encrypted at rest.

    Writing a row makes the table authoritative — the .env fallback stops
    applying the moment an active row exists.
    """
    from src.modules.platform import payment_credentials_service as creds_service

    _validate_paystack_keys(body.public_key, body.secret_key)

    row = await creds_service.get_credential(db, body.provider.value)
    if row is None:
        row = PlatformPaymentCredential(provider=body.provider.value)
        db.add(row)

    row.public_key_encrypted = encrypt_secret(body.public_key)
    row.secret_key_encrypted = encrypt_secret(body.secret_key)
    row.webhook_secret_encrypted = encrypt_secret(body.webhook_secret) if body.webhook_secret else None
    row.is_active = body.is_active
    # The keys changed, so any previous validation result no longer describes them.
    row.last_validated_at = None
    row.last_validation_error = None
    row.updated_at = datetime.now(timezone.utc)

    await db.commit()
    return await _credential_response(db)


@router.post("/payment-credentials/test", response_model=PlatformPaymentCredentialResponse)
async def test_platform_payment_credentials(
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Check the resolved keys against Paystack.

    Tests whatever is actually in force, table or .env. The result is only
    persisted when it came from a stored row — there is nowhere to record a
    validation against .env values.
    """
    import httpx

    from src.modules.platform import payment_credentials_service as creds_service

    keys = await creds_service.resolve_paystack_keys(db)
    if not keys.is_configured:
        raise HTTPException(
            status_code=400,
            detail=(
                "Platform payment credentials are not configured — no active row in "
                "platform_payment_credentials and no PLATFORM_BILLING_PAYSTACK_* values in the environment."
            ),
        )

    row = await creds_service.get_active_credential(db)
    try:
        async with httpx.AsyncClient(timeout=15.0, base_url="https://api.paystack.co") as client:
            response = await client.get(
                "/transaction",
                params={"perPage": 1},
                headers={"Authorization": f"Bearer {keys.secret_key}"},
            )
            response.raise_for_status()
    except Exception as exc:
        if row is not None:
            row.last_validation_error = str(exc)
            await db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if row is not None:
        row.last_validated_at = datetime.now(timezone.utc)
        row.last_validation_error = None
        await db.commit()
    return await _credential_response(db)
