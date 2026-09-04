from datetime import datetime, timezone
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
    OperatorInvoice,
    OperatorPaymentCredential,
    PaymentTransaction,
    PlatformOwner,
    Plan,
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
from src.schemas import LoginRequest, PlatformAdminCreate, PlatformOperatorCreate, PlatformOperatorStatusUpdate, RefreshRequest, TokenResponse
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

    operator = ISPOperator(
        name=body.name,
        slug=body.slug,
        contact_email=str(body.contact_email),
        contact_phone=body.contact_phone,
        status="approved",
        approved_at=datetime.now(timezone.utc),
        approved_by_platform_owner_id=owner.id,
        billing_status="active",
    )
    db.add(operator)
    await db.flush()

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

    operator.status = body.status
    operator.updated_at = datetime.now(timezone.utc)
    if body.status == "approved" and not operator.approved_at:
        operator.approved_at = datetime.now(timezone.utc)
        operator.approved_by_platform_owner_id = owner.id
    if body.status == "cancelled":
        operator.billing_status = "cancelled"
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
    rows = []
    for op in operators:
        outstanding = (
            await db.execute(
                select(OperatorInvoice).where(
                    OperatorInvoice.isp_operator_id == op.id,
                    OperatorInvoice.status.in_(["issued", "overdue"]),
                )
            )
        ).scalar_one_or_none()
        last_paid = (
            await db.execute(
                select(OperatorInvoice).where(
                    OperatorInvoice.isp_operator_id == op.id,
                    OperatorInvoice.status == "paid",
                ).order_by(OperatorInvoice.paid_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        rows.append({
            "id": str(op.id),
            "name": op.name,
            "slug": op.slug,
            "billing_status": op.billing_status,
            "monthly_fee_ghs": float(op.monthly_fee_ghs),
            "last_paid_at": last_paid.paid_at.isoformat() if last_paid and last_paid.paid_at else None,
            "next_due_at": outstanding.due_at.isoformat() if outstanding and outstanding.due_at else None,
            "outstanding_amount_ghs": float(outstanding.amount_ghs) if outstanding else 0,
            "outstanding_invoice_number": outstanding.invoice_number if outstanding else None,
        })
    return rows


@router.put("/operators/{operator_id}/billing")
async def update_operator_billing(
    operator_id: uuid.UUID,
    monthly_fee_ghs: Optional[Decimal] = None,
    extend_trial_days: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    operator = await db.get(ISPOperator, operator_id)
    if not operator:
        raise HTTPException(404, "Operator not found")
    if monthly_fee_ghs is not None:
        operator.monthly_fee_ghs = monthly_fee_ghs
    if extend_trial_days is not None and operator.trial_ends_at:
        from datetime import timedelta
        operator.trial_ends_at = operator.trial_ends_at + timedelta(days=extend_trial_days)
    await db.commit()
    return {"message": "Updated", "operator_id": str(operator.id)}


@router.put("/invoices/{invoice_id}/waive")
async def waive_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    from src.db.models import OperatorBillingEvent
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
