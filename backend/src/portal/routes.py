from datetime import datetime, timezone
from html import escape
import os
from types import SimpleNamespace
import uuid
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Form
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.base import get_db
from src.db.models import ISPOperator, PaymentTransaction, Plan, Router, Site, Voucher
from src.middleware.csp_pages import portal_csp_headers
from src.middleware.auth import SALES_PAUSED_DETAIL, sales_blocked
from src.middleware.rate_limit import enforce_rate_limit
from src.middleware.tenant_refs import load_owned
from types import SimpleNamespace

from src.modules.branding.preview_store import get_preview_draft
from src.modules.branding.service import build_branding
from src.modules.payments.dependencies import get_payment_service
from src.modules.payments.service import PaymentService
from src.modules.payments.types import PaymentMethod, PaymentStatus
from src.modules.platform.operator_status import hotspot_service_ended
from src.modules.vouchers.engine import code_key, damm_valid, is_numeric_code_key
from src.portal import login_limits
from src.utils.portal_token import decode_portal_preview_token, decode_portal_router_token
from src.schemas import (
    BrandingResponse,
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


async def _resolve_operator_from_token(db: AsyncSession, rt: str | None) -> tuple[str, str] | None:
    """Resolve (operator_id, site_id) from a signed router token, or None if the
    token is absent/invalid/unknown so the caller can fall back to gateway match."""
    router_id = decode_portal_router_token(rt)
    if not router_id:
        return None
    try:
        router_uuid = uuid.UUID(router_id)
    except (ValueError, TypeError):
        return None
    router_row = (
        await db.execute(select(Router).where(Router.id == router_uuid, Router.is_active == True))
    ).scalar_one_or_none()
    if not router_row:
        return None
    return str(router_row.isp_operator_id), (str(router_row.site_id) if router_row.site_id else "")


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


async def _resolve_operator(db: AsyncSession, rt: str | None, gateway: str | None) -> tuple[str, str]:
    """Prefer the signed router token; fall back to gateway-IP matching for legacy
    routers (or a malformed/missing token) rather than hard-erroring."""
    resolved = await _resolve_operator_from_token(db, rt)
    if resolved is not None:
        return resolved
    return await _resolve_operator_from_gateway(db, gateway)


_DRAFT_FIELDS = (
    "portal_display_name",
    "primary_color",
    "accent_color",
    "background_gradient_start",
    "portal_welcome_message",
    "portal_template",
    "portal_contact_phone",
    "portal_contact_email",
)


async def _resolve_branding(db: AsyncSession, rt: str | None) -> BrandingResponse:
    """Single resolution path for operator branding, keyed on the signed router
    token. Used both by the public GET /portal/branding/{rt} endpoint (the
    client-side fallback fetch) and, server-side, by the page handlers below so
    the initial HTML response already carries the operator's colours/logo/
    template/footer at first paint. A missing/invalid/unknown token (or no
    token at all — e.g. a legacy gateway-matched router) resolves to platform
    defaults; build_branding(None) never errors, so callers never need to
    special-case a missing operator.

    A settings-page preview token (distinct purpose, see utils/portal_token.py)
    resolves the same operator but overlays any in-progress draft from Redis
    (preview_store) on top of its persisted row — draft fields only; logo_url
    always comes from the real row, since logo upload already persists
    immediately and was never part of the draft. is_preview=True in either
    branch (draft present or not) so branding.js knows to start polling."""
    resolved = await _resolve_operator_from_token(db, rt)
    if resolved is not None:
        operator = (
            await db.execute(select(ISPOperator).where(ISPOperator.id == resolved[0]))
        ).scalar_one_or_none()
        return build_branding(operator)

    preview_operator_id = decode_portal_preview_token(rt)
    if preview_operator_id:
        operator = (
            await db.execute(select(ISPOperator).where(ISPOperator.id == preview_operator_id))
        ).scalar_one_or_none()
        if operator is None:
            return build_branding(None, is_preview=True)
        draft = await get_preview_draft(preview_operator_id)
        if draft:
            draft_operator = SimpleNamespace(
                name=operator.name,
                logo_url=operator.logo_url,
                **{
                    attr: draft.get(attr, getattr(operator, attr, None))
                    for attr in _DRAFT_FIELDS
                },
            )
            return build_branding(draft_operator, is_preview=True)
        return build_branding(operator, is_preview=True)

    return build_branding(None)


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
    rt: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    login_fallback = settings.mikrotik_gateway_login_url or "http://10.5.5.1/login"
    url = _decode_portal_value(url)
    link_orig = _normalize_hotspot_url(link_orig, url or "http://www.google.com")
    link_login_only = _normalize_hotspot_url(link_login_only, login_fallback)
    # If the signed token is valid, pin the router's site so the page can load its
    # plans directly. The token also rides forward (downstream calls re-resolve it).
    if not site_id:
        resolved = await _resolve_operator_from_token(db, rt)
        if resolved is not None:
            site_id = resolved[1]
    branding = await _resolve_branding(db, rt)
    nonce, csp_headers = portal_csp_headers(request)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "csp_nonce": nonce,
        "mac": mac,
        "ip": ip,
        "url": url,
        "gateway": gateway,
        "site_id": site_id,
        "link_login_only": link_login_only,
        "link_orig": link_orig,
        "rt": rt,
        "mikrotik_login_fallback": login_fallback,
        "branding": branding,
        "error": None,
    }, headers=csp_headers)


@router.get("/portal/pay", response_class=HTMLResponse)
async def portal_pay(
    request: Request,
    plan_id: str = Query(...),
    site_id: str = Query(...),
    gateway: str | None = Query(None),
    rt: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    branding = await _resolve_branding(db, rt)
    nonce, csp_headers = portal_csp_headers(request)
    return templates.TemplateResponse("pay.html", {
        "request": request,
        "csp_nonce": nonce,
        "plan_id": plan_id,
        "site_id": site_id,
        "gateway": gateway,
        "rt": rt,
        "branding": branding,
        "error": None,
    }, headers=csp_headers)


@router.get("/portal/success", response_class=HTMLResponse)
async def portal_success(
    request: Request,
    ref: str = Query(...),
    gateway: str | None = Query(None),
    rt: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    branding = await _resolve_branding(db, rt)
    nonce, csp_headers = portal_csp_headers(request)
    return templates.TemplateResponse("success.html", {
        "request": request,
        "csp_nonce": nonce,
        "ref": ref,
        "gateway": gateway,
        "rt": rt,
        "branding": branding,
        "error": None,
    }, headers=csp_headers)


@router.get("/portal/plans", response_model=list[PlanResponse])
async def portal_plans(
    request: Request,
    gateway: str | None = Query(None),
    site_id: str | None = Query(None),
    rt: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "portal:plans", limit=10, window_seconds=60)
    operator_id, gateway_site_id = await _resolve_operator(db, rt, gateway)
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
    gateway: str | None = Query(None),
    rt: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """The hotspot operator's own sites, for the location picker. It used to
    list every operator's sites (names, towns, street addresses) to anyone.
    The Branding page's live preview carries a preview token rather than a
    router token, so that lists the previewing operator's own sites."""
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "portal:sites", limit=10, window_seconds=60)
    from src.db.models import Site, Town

    resolved = await _resolve_operator_from_token(db, rt)
    preview_operator_id = None if resolved else decode_portal_preview_token(rt)
    if resolved:
        operator_id = resolved[0]
    elif preview_operator_id:
        operator_id = preview_operator_id
    else:
        operator_id, _ = await _resolve_operator_from_gateway(db, gateway)
    result = await db.execute(
        select(Site, Town).join(Town, Town.id == Site.town_id)
        .where(Site.isp_operator_id == uuid.UUID(str(operator_id)))
        .order_by(Town.name, Site.name)
    )
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


PORTAL_CARD_REFUSED = "Card payment isn't available here. Please pay with mobile money (MTN MoMo, Telecel Cash or AirtelTigo Cash)."


@router.post("/portal/initiate-payment", response_model=PortalInitiatePaymentResponse)
async def portal_initiate_payment(
    request: Request,
    body: PortalInitiatePaymentRequest,
    db: AsyncSession = Depends(get_db),
    payment_service: PaymentService = Depends(get_payment_service),
):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "portal:initiate", limit=10, window_seconds=60)

    # Captive-portal purchases are mobile money only. A card payment can't
    # finish from behind the hotspot: the walled garden blocks the provider's
    # checkout and 3-D Secure pages, and the return journey isn't built (see
    # the "Card payment on the captive portal (removed)" backlog note). The
    # option is gone from pay.html; this refuses a hand-crafted request before
    # anything is created. The provider code stays, unreachable from here.
    if body.payment_method.value == PaymentMethod.CARD.value:
        raise HTTPException(status_code=400, detail=PORTAL_CARD_REFUSED)

    operator_id, gateway_site_id = await _resolve_operator(db, body.rt, body.gateway)

    from src.db.models import ISPOperator
    _op = (await db.execute(select(ISPOperator).where(ISPOperator.id == operator_id))).scalar_one_or_none()
    if sales_blocked(_op):
        raise HTTPException(status_code=503, detail=SALES_PAUSED_DETAIL)

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

    if body.site_id is not None:
        # The hotspot operator's own site, or the same 404 as a missing one.
        await load_owned(db, Site, body.site_id, uuid.UUID(str(operator_id)), label="Site")
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


# Why the portal refuses a code, worded for the customer. RADIUS rejects
# exactly these, and the router turns a RADIUS reject into a silent reload of
# the login page, so the portal says it first. A refusal the customer can't
# act on (the operator's service has ended, or inconsistent data) gets
# RADIUS's own generic reject reply (freeradius/sites-enabled/default).
INVALID_CODE_DETAIL = "Invalid voucher code"
EXPIRED_DETAIL = "Code has expired"
USED_UP_DETAIL = "Code has been fully used"
DISABLED_DETAIL = "Code has been disabled by admin"
VOUCHER_REFUSED_DETAIL = "Access denied: voucher may be expired, exhausted, or disabled"
# A numeric reseller code whose Damm check digit is wrong: a typo, caught
# before any lookup, so it costs the customer none of their wrong-code allowance.
CHECK_DIGITS_DETAIL = "That code doesn't look right. Check the digits and try again."


class _UnknownCode(HTTPException):
    """No such code for this operator: the one refusal the failure limits count."""

    def __init__(self):
        super().__init__(status_code=400, detail=INVALID_CODE_DETAIL)


def _refuse(detail: str):
    raise HTTPException(status_code=400, detail=detail)


async def _voucher_for_login(db: AsyncSession, key: str, operator_id) -> Voucher:
    """The voucher RADIUS would accept on a router of operator_id, else a 400
    saying why not. ``key`` is the typed code through engine.code_key, matched
    on vouchers.code_key, so dashes, spaces and case don't matter. The same
    conditions as freeradius/sql.conf authorize_check_query, on the database
    clock, so the portal never hands out a password RADIUS then rejects.
    tests/test_portal_authenticate_flow.py runs that query on the same rows to
    keep the two in step."""
    row = (
        await db.execute(
            select(Voucher, Plan, ISPOperator.hotspot_service_ends_at, func.now())
            .join(Plan, Plan.id == Voucher.plan_id)
            .join(ISPOperator, ISPOperator.id == Voucher.isp_operator_id)
            # Another operator's code is as unknown here as a mistyped one.
            .where(Voucher.code_key == key, Voucher.isp_operator_id == operator_id)
        )
    ).one_or_none()
    if row is None:
        raise _UnknownCode()
    voucher, plan, service_ends_at, now = row
    # Operator-wide or bad data: nothing the customer can do about it.
    if hotspot_service_ended(service_ends_at, now):
        _refuse(VOUCHER_REFUSED_DETAIL)
    if plan.isp_operator_id != voucher.isp_operator_id or (plan.data_limit_mb is not None and voucher.data_used_mb is None):
        _refuse(VOUCHER_REFUSED_DETAIL)
    # The voucher's own state first, then what RADIUS checks beyond it.
    if voucher.status == "disabled":
        _refuse(DISABLED_DETAIL)
    if voucher.status == "expired":
        _refuse(EXPIRED_DETAIL)
    if voucher.status == "exhausted":
        _refuse(USED_UP_DETAIL)
    if voucher.status not in ("unused", "active"):
        _refuse(VOUCHER_REFUSED_DETAIL)
    if plan.is_active is not True:
        _refuse(DISABLED_DETAIL)
    if voucher.expires_at is not None and voucher.expires_at <= now:  # before expire_vouchers marks it
        _refuse(EXPIRED_DETAIL)
    # A reseller code past its sell-by. RADIUS doesn't read redeem_by; it
    # refuses once expire_vouchers has marked the code expired, seconds later.
    # Until then this is the only way to the password, so it is enough.
    if voucher.status == "unused" and voucher.redeem_by is not None and voucher.redeem_by <= now:
        _refuse(EXPIRED_DETAIL)
    if plan.data_limit_mb is not None and voucher.data_used_mb >= plan.data_limit_mb:
        _refuse(USED_UP_DETAIL)
    return voucher


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
    rt: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    key = code_key(code)
    if not key:
        raise HTTPException(status_code=400, detail="Please enter a voucher code")
    if is_numeric_code_key(key) and not damm_valid(key):
        raise HTTPException(status_code=400, detail=CHECK_DIGITS_DETAIL)

    operator_id, site_id = await _resolve_operator(db, rt, gateway)

    # Wrong-code limits (src/portal/login_limits.py): checked before the
    # lookup, counted only when the code doesn't exist.
    attempt = login_limits.Attempt(
        operator_id=str(operator_id), ip=request.client.host if request.client else "",
        mac=login_limits.normalize_mac(mac), site_id=login_limits.site_or_none(site_id),
    )
    limits = await login_limits.current_limits()
    await login_limits.check(attempt, limits)
    try:
        voucher = await _voucher_for_login(db, key, operator_id)
    except _UnknownCode:
        await login_limits.record_failure(attempt, limits)
        raise

    return PortalAuthenticateResponse(
        success=True,
        username=voucher.username,
        password=voucher.password,
    )


@router.get("/portal/mikrotik/login-template", response_class=HTMLResponse)
async def portal_mikrotik_login_template(rt: str = Query(None)):
    settings = get_settings()
    base = settings.effective_portal_public_base_url
    if not base:
        raise HTTPException(status_code=500, detail="Portal public base URL is not configured")

    # rt is a signed router token baked in at provisioning time. It is a static
    # string (no RouterOS variables), so it survives /tool fetch unchanged and
    # rides along on every client redirect to identify this router/operator.
    rt_field = f'\n    <input type="hidden" name="rt" value="{escape(rt, quote=True)}">' if rt else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Redirecting</title>
</head>
<body>
  <form id="portal-redirect" action="{base}/portal/login" method="get">{rt_field}
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


@router.get("/portal/branding/{rt}", response_model=BrandingResponse)
async def portal_branding(rt: str, db: AsyncSession = Depends(get_db)):
    """Public branding for the captive portal, keyed on the signed router token.
    A missing/invalid/unknown token resolves to platform-wide defaults — this must
    never error, so the portal always has something to render. Same resolution
    _resolve_branding() also runs server-side when login/pay/success are served,
    so the client-side fetch this endpoint answers reapplies identical values
    rather than a second implementation of the lookup."""
    return await _resolve_branding(db, rt)


@router.get("/portal/statics/{file_path:path}")
async def serve_portal_static(file_path: str):
    full_path = os.path.join(PORTAL_DIR, "statics", file_path)
    if os.path.exists(full_path):
        return FileResponse(full_path)
    raise HTTPException(status_code=404, detail="File not found")
