from datetime import datetime, timezone
import os
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Form
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select, cast
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.base import get_db
from src.db.models import PaymentTransaction, Plan, Router, Site, Voucher
from src.middleware.rate_limit import enforce_rate_limit
from src.modules.payments.dependencies import get_payment_service
from src.modules.payments.service import PaymentService
from src.modules.payments.types import PaymentMethod, PaymentStatus
from src.schemas import (
    PortalContinuePaymentRequest,
    PortalAuthenticateResponse,
    PlanResponse,
    PortalInitiatePaymentRequest,
    PortalInitiatePaymentResponse,
    PortalPlanSummary,
    PortalPaymentStatusResponse,
)

router = APIRouter(tags=["captive-portal"])

PORTAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "..", "portal")
templates = Jinja2Templates(directory=PORTAL_DIR)


def _payment_redirect_url(tx: PaymentTransaction) -> str | None:
    payload = tx.provider_payload if isinstance(tx.provider_payload, dict) else {}
    return payload.get("authorization_url") or payload.get("url")


def _decode_portal_value(value: str | None) -> str | None:
    if value is None:
        return None
    decoded = value
    for _ in range(2):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    decoded = decoded.strip()
    return decoded or None


def _normalize_hotspot_url(value: str | None, fallback: str) -> str:
    decoded = _decode_portal_value(value)
    if not decoded:
        return fallback
    parsed = urlparse(decoded)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return decoded
    return fallback


async def _resolve_operator_from_gateway(db: AsyncSession, gateway: str | None) -> tuple[str, str]:
    if not gateway:
        raise HTTPException(status_code=400, detail="Gateway is required")
    router_row = (
        await db.execute(
            select(Router).where(Router.ip_address == cast(gateway, INET), Router.is_active == True)
        )
    ).scalar_one_or_none()
    if not router_row:
        raise HTTPException(status_code=404, detail="This hotspot is not configured.")
    return str(router_row.isp_operator_id), str(router_row.site_id)


@router.get("/portal/login", response_class=HTMLResponse)
async def portal_login(
    request: Request,
    mac: str = Query(None),
    ip: str = Query(None),
    url: str = Query(None),
    gateway: str = Query(None),
    site_id: str = Query(None),
    link_login_only: str = Query(None),
    link_orig: str = Query(None),
):
    settings = get_settings()
    login_fallback = settings.mikrotik_gateway_login_url or "http://10.5.5.1/login"
    url = _decode_portal_value(url)
    link_orig = _normalize_hotspot_url(link_orig, url or "http://www.google.com")
    link_login_only = _normalize_hotspot_url(link_login_only, login_fallback)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "mac": mac,
        "ip": ip,
        "url": url,
        "gateway": gateway,
        "site_id": site_id,
        "link_login_only": link_login_only,
        "link_orig": link_orig,
        "mikrotik_login_fallback": login_fallback,
        "error": None,
    })


@router.get("/portal/pay", response_class=HTMLResponse)
async def portal_pay(
    request: Request,
    plan_id: str = Query(...),
    site_id: str = Query(...),
    gateway: str | None = Query(None),
):
    return templates.TemplateResponse("pay.html", {
        "request": request,
        "plan_id": plan_id,
        "site_id": site_id,
        "gateway": gateway,
        "error": None,
    })


@router.get("/portal/success", response_class=HTMLResponse)
async def portal_success(
    request: Request,
    ref: str = Query(...),
    gateway: str | None = Query(None),
):
    return templates.TemplateResponse("success.html", {
        "request": request,
        "ref": ref,
        "gateway": gateway,
        "error": None,
    })


@router.get("/portal/plans", response_model=list[PlanResponse])
async def portal_plans(
    request: Request,
    gateway: str | None = Query(None),
    site_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "portal:plans", limit=10, window_seconds=60)
    operator_id, gateway_site_id = await _resolve_operator_from_gateway(db, gateway)
    resolved_site_id = site_id or gateway_site_id
    stmt = select(Plan).where(Plan.is_active == True, Plan.isp_operator_id == operator_id)
    if resolved_site_id:
        stmt = stmt.where(or_(Plan.site_id == resolved_site_id, Plan.site_id.is_(None)))
    else:
        stmt = stmt.where(Plan.site_id.is_(None))
    result = await db.execute(stmt.order_by(Plan.price_ghs.asc()))
    return result.scalars().all()


@router.get("/portal/sites")
async def portal_sites(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "portal:sites", limit=10, window_seconds=60)
    from src.db.models import Site, Town

    result = await db.execute(select(Site, Town).join(Town, Town.id == Site.town_id).order_by(Town.name, Site.name))
    rows = result.all()
    return [
        {
            "id": str(site.id),
            "name": site.name,
            "town_name": town.name,
            "region": town.region,
            "address": site.address,
        }
        for site, town in rows
    ]


@router.post("/portal/initiate-payment", response_model=PortalInitiatePaymentResponse)
async def portal_initiate_payment(
    request: Request,
    body: PortalInitiatePaymentRequest,
    db: AsyncSession = Depends(get_db),
    payment_service: PaymentService = Depends(get_payment_service),
):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "portal:initiate", limit=10, window_seconds=60)

    operator_id, gateway_site_id = await _resolve_operator_from_gateway(db, body.gateway)

    from src.db.models import ISPOperator
    _op = (await db.execute(select(ISPOperator).where(ISPOperator.id == operator_id))).scalar_one_or_none()
    if _op and _op.status == "suspended":
        raise HTTPException(status_code=503, detail="This hotspot is temporarily unavailable for new purchases")

    plan = (
        await db.execute(
            select(Plan).where(
                Plan.id == body.plan_id,
                Plan.isp_operator_id == operator_id,
                Plan.is_active == True,
            )
        )
    ).scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    resolved_site_id = body.site_id or plan.site_id or gateway_site_id
    if resolved_site_id is None:
        raise HTTPException(status_code=400, detail="Site is required to purchase this plan")
    if plan.site_id is not None and str(plan.site_id) != str(resolved_site_id):
        raise HTTPException(status_code=400, detail="Selected plan does not belong to the chosen site")

    try:
        tx = await payment_service.create_pending_transaction(
            db,
            plan_id=str(body.plan_id),
            site_id=str(resolved_site_id),
            isp_operator_id=operator_id,
            amount_ghs=plan.price_ghs,
            payment_method=PaymentMethod(body.payment_method.value),
            phone_number=body.phone,
            ip_address=client_ip,
        )
        updated = await payment_service.initiate_payment(db, transaction_id=str(tx.id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PortalInitiatePaymentResponse(
        internal_reference=updated.internal_reference,
        status=updated.status,
        next_action=updated.next_action,
        display_message=updated.display_message,
        payment_channel=updated.payment_channel,
        redirect_url=_payment_redirect_url(updated),
    )


@router.post("/portal/continue-payment", response_model=PortalInitiatePaymentResponse)
async def portal_continue_payment(
    request: Request,
    body: PortalContinuePaymentRequest,
    db: AsyncSession = Depends(get_db),
    payment_service: PaymentService = Depends(get_payment_service),
):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "portal:continue", limit=20, window_seconds=60)

    try:
        updated = await payment_service.continue_payment(
            db,
            internal_reference=body.ref,
            otp=body.otp,
            phone=body.phone,
            pin=body.pin,
            birthday=body.birthday,
            address=body.address,
            city=body.city,
            state=body.state,
            zip_code=body.zip_code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return PortalInitiatePaymentResponse(
        internal_reference=updated.internal_reference,
        status=updated.status,
        next_action=updated.next_action,
        display_message=updated.display_message,
        payment_channel=updated.payment_channel,
        redirect_url=_payment_redirect_url(updated),
    )


@router.get("/portal/payment-status", response_model=PortalPaymentStatusResponse)
async def portal_payment_status(
    request: Request,
    ref: str = Query(...),
    db: AsyncSession = Depends(get_db),
    payment_service: PaymentService = Depends(get_payment_service),
):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "portal:status", limit=30, window_seconds=60)
    try:
        tx = await payment_service.get_transaction_by_reference(db, ref)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if tx.status == PaymentStatus.PENDING.value:
        try:
            tx = await payment_service.refresh_transaction_status(db, tx=tx, force=False)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    voucher_code = None
    if tx.status == PaymentStatus.SUCCESS.value and tx.voucher_id:
        voucher = (
            await db.execute(select(Voucher).where(Voucher.id == tx.voucher_id, Voucher.isp_operator_id == tx.isp_operator_id))
        ).scalar_one_or_none()
        voucher_code = voucher.code if voucher else None
    plan = (
        await db.execute(select(Plan).where(Plan.id == tx.plan_id, Plan.isp_operator_id == tx.isp_operator_id))
    ).scalar_one_or_none()
    plan_summary = PortalPlanSummary.model_validate(plan) if plan else None
    return PortalPaymentStatusResponse(
        status=tx.status,
        next_action=tx.next_action,
        display_message=tx.display_message,
        voucher_code=voucher_code,
        failure_reason=tx.failure_reason,
        payment_channel=tx.payment_channel,
        plan=plan_summary,
    )


@router.post("/portal/authenticate", response_model=PortalAuthenticateResponse)
async def portal_authenticate(
    request: Request,
    code: str = Form(None),
    mac: str | None = Form(None),
    ip: str | None = Form(None),
    url: str | None = Form(None),
    gateway: str | None = Form(None),
    site_id: str | None = Form(None),
    link_login_only: str | None = Form(None),
    link_orig: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if not code:
        raise HTTPException(status_code=400, detail="Please enter a voucher code")

    operator_id, _ = await _resolve_operator_from_gateway(db, gateway)

    code = code.strip().upper()
    result = await db.execute(select(Voucher).where(Voucher.code == code, Voucher.isp_operator_id == operator_id))
    voucher = result.scalar_one_or_none()

    if not voucher:
        raise HTTPException(status_code=400, detail="Invalid voucher code")
    if voucher.status == "expired":
        raise HTTPException(status_code=400, detail="Code has expired")
    if voucher.status == "exhausted":
        raise HTTPException(status_code=400, detail="Code has been fully used")
    if voucher.status == "disabled":
        raise HTTPException(status_code=400, detail="Code has been disabled by admin")

    return PortalAuthenticateResponse(
        success=True,
        username=voucher.username,
        password=voucher.password,
    )


@router.get("/portal/mikrotik/login-template", response_class=HTMLResponse)
async def portal_mikrotik_login_template():
    settings = get_settings()
    base = settings.effective_portal_public_base_url
    if not base:
        raise HTTPException(status_code=500, detail="Portal public base URL is not configured")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Redirecting</title>
</head>
<body>
  <form id="portal-redirect" action="{base}/portal/login" method="get">
    <input type="hidden" name="mac" value="$(mac-esc)">
    <input type="hidden" name="ip" value="$(ip-esc)">
    <input type="hidden" name="gateway" value="$(server-address)">
    <input type="hidden" name="url" value="$(link-orig-esc)">
    <input type="hidden" name="link_orig" value="$(link-orig-esc)">
    <input type="hidden" name="link_login_only" value="$(link-login-only-esc)">
  </form>
  <script>
    document.getElementById('portal-redirect').submit();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@router.get("/portal/statics/{file_path:path}")
async def serve_portal_static(file_path: str):
    full_path = os.path.join(PORTAL_DIR, "statics", file_path)
    if os.path.exists(full_path):
        return FileResponse(full_path)
    raise HTTPException(status_code=404, detail="File not found")
