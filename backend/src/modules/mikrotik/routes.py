from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import CoAEvent, ConfigTemplate, Router, RouterCredential, RouterMetric, RouterProvisionLog, Session, Site, Town, Voucher
from src.middleware.auth import TenantContext, get_admin_tenant_context, get_current_user, require_active_operator
from src.modules.mikrotik.api_service import MikroTikAPIService, MikroTikOperationError, RouterCredentialsMissingError
from src.modules.mikrotik.diagnostics import MikroTikDiagnosticsService
from src.modules.mikrotik.provisioner import MikroTikProvisioner
from src.modules.mikrotik.removal import RouterAlreadyRemovedError, remove_router
from src.modules.mikrotik.template_engine import ConfigTemplateService, TemplateValidationError
from src.modules.mikrotik.types import (
    ApplyTemplateRequest,
    ConfigTemplateRequest,
    ConfigTemplateResponse,
    ConfigTemplateUpdateRequest,
    ConfirmActionRequest,
    ConnectionTestRequest,
    ConnectionTestResult,
    DisconnectUserRequest,
    ProvisionRequest,
    ProvisionStartResponse,
    ProvisionStatusResponse,
    RouterCredentialsRequest,
    RouterCredentialsResponse,
    RouterMetricResponse,
    RouterRemovalSummary,
    TempInterfacesRequest,
)
from src.modules.wireguard.service import WireGuardService
from src.utils.encryption import encrypt_secret
from src.utils.freeradius_reload import reload_freeradius_clients

router = APIRouter(prefix="/admin", tags=["mikrotik-admin"])
service = MikroTikAPIService()
diagnostics_service = MikroTikDiagnosticsService()
wg_service = WireGuardService()
provisioner = MikroTikProvisioner()
template_service = ConfigTemplateService()


@router.post("/routers/test-connection", response_model=ConnectionTestResult)
async def test_router_connection(body: ConnectionTestRequest, _=Depends(get_current_user)):
    result = await service.test_connection_async(
        host=body.host,
        port=body.port,
        username=body.username,
        password=body.password,
        use_ssl=body.use_ssl,
    )
    if result.success:
        _remember_verified_connection(body.host, body.port, body.username)
    return result


@router.post("/routers/temp-interfaces")
async def temp_interfaces(body: TempInterfacesRequest, _=Depends(get_current_user)):
    try:
        items = await service.list_interfaces_temp(
            host=body.host,
            port=body.port,
            username=body.username,
            password=body.password,
            use_ssl=body.use_ssl,
        )
        return [item.model_dump() for item in items]
    except MikroTikOperationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/sites")
async def admin_sites(db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    result = await db.execute(
        select(Site, Town)
        .join(Town, Town.id == Site.town_id)
        .where(Site.isp_operator_id == tenant.isp_operator_id, Town.isp_operator_id == tenant.isp_operator_id)
        .order_by(Town.name, Site.name)
    )
    rows = result.all()
    return [
        {
            "id": str(site.id),
            "name": site.name,
            "town_id": str(site.town_id),
            "town_name": town.name,
            "region": town.region,
            "address": site.address,
        }
        for site, town in rows
    ]


@router.post("/routers/onboard")
async def onboard_router(payload: dict, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_active_operator)):
    # API credentials are now optional — an operator can register a router before
    # it is reachable (e.g. behind NAT) and set up a WireGuard tunnel afterwards.
    # Only the basic identity fields are required.
    required = ["site_id", "name", "nas_identifier", "nas_secret"]
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing required fields: {', '.join(missing)}")

    site = (
        await db.execute(select(Site).where(Site.id == payload["site_id"], Site.isp_operator_id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found")

    router_row = Router(
        isp_operator_id=tenant.isp_operator_id,
        site_id=payload["site_id"],
        name=payload["name"],
        # ip_address is an optional display/label field only. Connectivity always
        # comes from the WireGuard tunnel set up in the next wizard step — the
        # RouterOS API is never reached over this address.
        ip_address=payload.get("ip_address") or None,
        nas_identifier=payload["nas_identifier"],
        nas_secret=encrypt_secret(payload["nas_secret"]),
        nas_secret_plain=payload["nas_secret"],
        is_active=bool(payload.get("is_active", True)),
    )
    db.add(router_row)
    await db.commit()
    asyncio.get_running_loop().run_in_executor(None, reload_freeradius_clients)
    await db.refresh(router_row)

    # VPN-only onboarding: a brand-new router is only reachable once its WireGuard
    # tunnel is up, so credentials and provisioning are handled by the later
    # wizard steps via the dedicated /credentials and /provision endpoints (which
    # connect over the tunnel). Onboard just persists the basic router record.
    return {"router_id": str(router_row.id), "log_id": None}


@router.get("/routers")
async def admin_router_list(
    include_removed: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    # Removed (is_active=false) routers are hidden by default — they're retired
    # fleet, not working inventory — but never deleted, so ?include_removed=true
    # (and direct navigation to /routers/{id}) still reaches their full history.
    conditions = [Router.isp_operator_id == tenant.isp_operator_id, Site.isp_operator_id == tenant.isp_operator_id]
    if not include_removed:
        conditions.append(Router.is_active.is_(True))
    result = await db.execute(
        select(Router, Site, RouterCredential)
        .join(Site, Site.id == Router.site_id)
        .join(RouterCredential, RouterCredential.router_id == Router.id, isouter=True)
        .where(*conditions)
        .order_by(Router.name.asc())
    )
    rows = result.all()
    return [
        {
            "id": str(router_row.id),
            "site_id": str(router_row.site_id),
            "site_name": site.name,
            "name": router_row.name,
            "ip_address": str(router_row.ip_address) if router_row.ip_address else None,
            "nas_identifier": router_row.nas_identifier,
            "is_active": bool(router_row.is_active),
            "is_online": bool(router_row.is_online),
            "last_seen_at": router_row.last_seen_at.isoformat() if router_row.last_seen_at else None,
            "connection_status": credentials.connection_status if credentials else "unknown",
            "last_connected_at": credentials.last_connected_at.isoformat() if credentials and credentials.last_connected_at else None,
            # Only meaningful when is_active is false — see Router.removed_at.
            # False here (not None) is the "may still have an online customer"
            # signal the fleet list surfaces without opening the router.
            "removal_router_reachable": router_row.removal_router_reachable,
        }
        for router_row, site, credentials in rows
    ]


@router.get("/routers/{router_id}")
async def admin_router_detail(router_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    result = await db.execute(
        select(Router, Site, RouterCredential)
        .join(Site, Site.id == Router.site_id)
        .join(RouterCredential, RouterCredential.router_id == Router.id, isouter=True)
        .where(Router.id == router_id, Router.isp_operator_id == tenant.isp_operator_id, Site.isp_operator_id == tenant.isp_operator_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Router not found")
    router_row, site, credentials = row
    latest_metric = (
        await db.execute(select(RouterMetric).where(RouterMetric.router_id == router_id).order_by(RouterMetric.collected_at.desc()))
    ).scalars().first()
    return {
        "id": str(router_row.id),
        "site_id": str(router_row.site_id),
        "site_name": site.name,
        "name": router_row.name,
        "ip_address": str(router_row.ip_address) if router_row.ip_address else None,
        "nas_identifier": router_row.nas_identifier,
        "is_active": bool(router_row.is_active),
        "is_online": bool(router_row.is_online),
        "last_seen_at": router_row.last_seen_at.isoformat() if router_row.last_seen_at else None,
        "connection_status": credentials.connection_status if credentials else "unknown",
        "last_connected_at": credentials.last_connected_at.isoformat() if credentials and credentials.last_connected_at else None,
        # Durable removal outcome (Router.removed_at etc — see models.py comment
        # and migration 040). All null when the router has never been removed.
        "removal": {
            "removed_at": router_row.removed_at.isoformat() if router_row.removed_at else None,
            "router_reachable": router_row.removal_router_reachable,
            "sessions_disconnected": router_row.removal_sessions_disconnected,
            "sessions_failed": router_row.removal_sessions_failed,
        },
        "wireguard": {
            "enabled": bool(router_row.wg_enabled),
            "connected": bool(router_row.wg_is_connected),
            "tunnel_ip": str(router_row.wg_tunnel_ip) if router_row.wg_tunnel_ip else None,
            "last_handshake_at": router_row.wg_last_handshake_at.isoformat() if router_row.wg_last_handshake_at else None,
        },
        "credentials": {
            "api_username": credentials.api_username if credentials else "",
            "api_port": int(credentials.api_port) if credentials else 8728,
            "use_ssl": bool(credentials.use_ssl) if credentials else False,
        },
        "system_info": {
            "board_name": latest_metric.board_name if latest_metric else None,
            "ros_version": latest_metric.ros_version if latest_metric else None,
            "uptime_seconds": latest_metric.uptime_seconds if latest_metric else None,
            "cpu_load_percent": latest_metric.cpu_load_percent if latest_metric else None,
            "memory_used_percent": latest_metric.memory_used_percent if latest_metric else None,
        },
    }


@router.delete("/routers/{router_id}", response_model=RouterRemovalSummary)
async def remove_router_endpoint(
    router_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    """Soft-delete: is_active=false. Never a hard row delete — see removal.py
    for why (Session/RouterMetric/RouterProvisionLog/CoAEvent/RouterCredential
    all reference router_id with no ON DELETE, so a hard delete would fail
    with a FK violation for any router with real history, and silently
    destroy it for the rare one that has none)."""
    router_row = await _ensure_router_in_tenant(db, router_id, tenant.isp_operator_id)
    try:
        return await remove_router(db, router_row, tenant.isp_operator_id, api_service=service, wg_service=wg_service)
    except RouterAlreadyRemovedError:
        raise HTTPException(status_code=409, detail="Router has already been removed")


@router.post("/routers/{router_id}/credentials", response_model=RouterCredentialsResponse, status_code=201)
async def create_router_credentials(router_id: uuid.UUID, body: RouterCredentialsRequest, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    router_row = (
        await db.execute(select(Router).where(Router.id == router_id, Router.isp_operator_id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if router_row is None:
        raise HTTPException(status_code=404, detail="Router not found")
    existing = (await db.execute(select(RouterCredential).where(RouterCredential.router_id == router_id))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Router credentials already exist")
    credentials = RouterCredential(
        router_id=router_id,
        api_username=body.api_username,
        api_password_encrypted=encrypt_secret(body.api_password),
        api_port=body.api_port,
        use_ssl=body.use_ssl,
        connection_status="unknown",
    )
    db.add(credentials)
    await db.commit()
    await db.refresh(credentials)
    return _serialize_credentials(credentials)


@router.put("/routers/{router_id}/credentials", response_model=RouterCredentialsResponse)
async def update_router_credentials(router_id: uuid.UUID, body: RouterCredentialsRequest, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    # Upsert: a router onboarded via the VPN flow has no RouterCredential row yet,
    # so the edit modal must be able to *create* the credentials here, not only
    # update them. (Previously this 404'd, and the frontend's apiCall swallows
    # non-2xx responses — so the save appeared to succeed while provisioning kept
    # failing with "Router credentials are required".)
    router_row = (
        await db.execute(select(Router).where(Router.id == router_id, Router.isp_operator_id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if router_row is None:
        raise HTTPException(status_code=404, detail="Router not found")
    credentials = (
        await db.execute(select(RouterCredential).where(RouterCredential.router_id == router_id))
    ).scalar_one_or_none()
    if credentials is None:
        credentials = RouterCredential(router_id=router_id, connection_status="unknown")
        db.add(credentials)
    credentials.api_username = body.api_username
    credentials.api_password_encrypted = encrypt_secret(body.api_password)
    credentials.api_port = body.api_port
    credentials.use_ssl = body.use_ssl
    await db.commit()
    await db.refresh(credentials)
    return _serialize_credentials(credentials)


@router.post("/routers/{router_id}/provision", response_model=ProvisionStartResponse)
async def provision_router(router_id: uuid.UUID, body: ProvisionRequest, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_active_operator)):
    router_row = (
        await db.execute(select(Router).where(Router.id == router_id, Router.isp_operator_id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if router_row is None:
        raise HTTPException(status_code=404, detail="Router not found")
    try:
        await provisioner.ensure_router_credentials(str(router_id))
    except RouterCredentialsMissingError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    log_row = RouterProvisionLog(
        router_id=router_id,
        triggered_by=str(tenant.user_id),
        action="provision",
        status="running",
        started_at=datetime.now(timezone.utc),
        commands_executed=[],
    )
    db.add(log_row)
    await db.commit()
    await db.refresh(log_row)
    provisioner.launch_provision(
        router_id=str(router_id),
        log_id=str(log_row.id),
        dns_name=body.dns_name,
        hotspot_interface=body.hotspot_interface,
        template_id=body.template_id,
    )
    return ProvisionStartResponse(log_id=str(log_row.id))


@router.get("/routers/{router_id}/provision-status/{log_id}", response_model=ProvisionStatusResponse)
async def provision_status(router_id: uuid.UUID, log_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    log_row = (
        await db.execute(
            select(RouterProvisionLog)
            .join(Router, Router.id == RouterProvisionLog.router_id)
            .where(
                RouterProvisionLog.id == log_id,
                RouterProvisionLog.router_id == router_id,
                Router.isp_operator_id == tenant.isp_operator_id,
            )
        )
    ).scalar_one_or_none()
    if log_row is None:
        raise HTTPException(status_code=404, detail="Provision log not found")
    return _serialize_provision_status(log_row)


@router.get("/routers/{router_id}/diagnostics")
async def router_diagnostics(router_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    await _ensure_router_in_tenant(db, router_id, tenant.isp_operator_id)
    return (await diagnostics_service.run(str(router_id))).model_dump()


@router.get("/routers/{router_id}/interfaces")
async def router_interfaces(router_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    await _ensure_router_in_tenant(db, router_id, tenant.isp_operator_id)
    try:
        return [item.model_dump() for item in await service.get_interfaces(str(router_id))]
    except RouterCredentialsMissingError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except MikroTikOperationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/routers/{router_id}/active-sessions")
async def router_active_sessions(router_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    await _ensure_router_in_tenant(db, router_id, tenant.isp_operator_id)
    try:
        return [item.model_dump() for item in await service.get_active_hotspot_users(str(router_id))]
    except RouterCredentialsMissingError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except MikroTikOperationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/routers/{router_id}/disconnect-user")
async def disconnect_router_user(
    router_id: uuid.UUID,
    body: DisconnectUserRequest,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    await _ensure_router_in_tenant(db, router_id, tenant.isp_operator_id)
    voucher_id = body.voucher_id
    session_row = None
    if voucher_id is None and body.username:
        voucher = (
            await db.execute(select(Voucher).where(Voucher.username == body.username, Voucher.isp_operator_id == tenant.isp_operator_id))
        ).scalar_one_or_none()
        voucher_id = str(voucher.id) if voucher else None
    if voucher_id is None:
        raise HTTPException(status_code=422, detail="voucher_id or username matching a voucher is required for audit logging")
    if body.username:
        session_row = (
            await db.execute(
                select(Session)
                .where(
                    Session.router_id == router_id,
                    Session.username == body.username,
                    Session.isp_operator_id == tenant.isp_operator_id,
                    Session.stopped_at.is_(None),
                )
                .order_by(Session.started_at.desc())
            )
        ).scalars().first()
    # disconnect_hotspot_user also clears the client's hotspot cookie, so the
    # router can't silently log it back in. The audit event is written from the
    # real outcome (never left "pending", which the backstop treats as in-flight).
    error = None
    try:
        result = await service.disconnect_hotspot_user(str(router_id), body.active_id)
    except Exception as exc:
        error = exc
    db.add(
        CoAEvent(
            session_id=session_row.id if session_row else None,
            voucher_id=voucher_id,
            router_id=router_id,
            isp_operator_id=tenant.isp_operator_id,
            event_type="disconnect",
            status="failed" if error else "confirmed",
            error_message=str(error)[:500] if error else None,
            # A failed manual click is surfaced to the operator directly; mark it
            # retry-exhausted so coa_retry doesn't fire unrequested CoAs later.
            attempt_count=3 if error else 1,
            last_attempted_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    if error:
        raise error
    return result.model_dump()


@router.get("/routers/{router_id}/metrics", response_model=list[RouterMetricResponse])
async def router_metrics(router_id: uuid.UUID, hours: int = Query(default=24, ge=1, le=168), db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    await _ensure_router_in_tenant(db, router_id, tenant.isp_operator_id)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = await db.execute(
        select(RouterMetric)
        .where(RouterMetric.router_id == router_id, RouterMetric.collected_at >= cutoff)
        .order_by(RouterMetric.collected_at.asc())
    )
    rows = result.scalars().all()
    return [_serialize_metric(row) for row in rows]


@router.get("/routers/{router_id}/provision-logs")
async def router_provision_logs(router_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    await _ensure_router_in_tenant(db, router_id, tenant.isp_operator_id)
    result = await db.execute(
        select(RouterProvisionLog)
        .where(RouterProvisionLog.router_id == router_id)
        .order_by(RouterProvisionLog.created_at.desc())
    )
    rows = result.scalars().all()
    return [
        {
            "id": str(row.id),
            "action": row.action,
            "status": row.status,
            "triggered_by": row.triggered_by,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "error_message": row.error_message,
            "commands_executed": list(row.commands_executed or []),
        }
        for row in rows
    ]


@router.post("/routers/{router_id}/reboot", response_model=ProvisionStartResponse)
async def reboot_router(router_id: uuid.UUID, body: ConfirmActionRequest, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    if body.confirm is not True:
        raise HTTPException(status_code=422, detail="confirm=true is required")
    await _ensure_router_in_tenant(db, router_id, tenant.isp_operator_id)
    await provisioner.ensure_router_credentials(str(router_id))
    log_row = RouterProvisionLog(
        router_id=router_id,
        triggered_by=str(tenant.user_id),
        action="reboot",
        status="running",
        started_at=datetime.now(timezone.utc),
        commands_executed=[],
    )
    db.add(log_row)
    await db.commit()
    await db.refresh(log_row)

    async def _background_reboot():
        try:
            result = await service.reboot_router(str(router_id))
            await provisioner._append_commands(str(log_row.id), result.commands_executed)
            await provisioner._mark_log_complete(str(log_row.id), status="success")
        except Exception as exc:
            await provisioner._mark_log_complete(str(log_row.id), status="failed", error_message=str(exc))

    import asyncio

    asyncio.create_task(_background_reboot())
    return ProvisionStartResponse(log_id=str(log_row.id))


@router.post("/routers/{router_id}/apply-template", response_model=ProvisionStartResponse)
async def apply_template(router_id: uuid.UUID, body: ApplyTemplateRequest, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(require_active_operator)):
    await _ensure_router_in_tenant(db, router_id, tenant.isp_operator_id)
    await provisioner.ensure_router_credentials(str(router_id))
    template = (
        await db.execute(
            select(ConfigTemplate).where(
                ConfigTemplate.id == body.template_id,
                (ConfigTemplate.isp_operator_id == tenant.isp_operator_id) | (ConfigTemplate.isp_operator_id.is_(None)),
            )
        )
    ).scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="Config template not found")
    log_row = RouterProvisionLog(
        router_id=router_id,
        triggered_by=str(tenant.user_id),
        action="apply_template",
        status="running",
        started_at=datetime.now(timezone.utc),
        commands_executed=[],
    )
    db.add(log_row)
    await db.commit()
    await db.refresh(log_row)
    provisioner.launch_apply_template(router_id=str(router_id), log_id=str(log_row.id), template_id=body.template_id, dns_name=body.dns_name)
    return ProvisionStartResponse(log_id=str(log_row.id))


@router.get("/config-templates", response_model=list[ConfigTemplateResponse])
async def list_config_templates(db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    rows = await template_service.list_templates(db, tenant.isp_operator_id)
    return [_serialize_template(row) for row in rows]


@router.post("/config-templates", response_model=ConfigTemplateResponse, status_code=201)
async def create_config_template(body: ConfigTemplateRequest, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    try:
        template = await template_service.create_template(db, body, tenant.isp_operator_id)
        return _serialize_template(template)
    except TemplateValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.put("/config-templates/{template_id}", response_model=ConfigTemplateResponse)
async def update_config_template(template_id: uuid.UUID, body: ConfigTemplateUpdateRequest, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    template = (
        await db.execute(
            select(ConfigTemplate).where(
                ConfigTemplate.id == template_id,
                ConfigTemplate.isp_operator_id == tenant.isp_operator_id,
            )
        )
    ).scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="Config template not found")
    try:
        template = await template_service.update_template(db, template, body, tenant.isp_operator_id)
        return _serialize_template(template)
    except TemplateValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.delete("/config-templates/{template_id}", status_code=204)
async def delete_config_template(template_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    template = (
        await db.execute(
            select(ConfigTemplate).where(
                ConfigTemplate.id == template_id,
                ConfigTemplate.isp_operator_id == tenant.isp_operator_id,
            )
        )
    ).scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="Config template not found")
    await db.delete(template)
    await db.commit()


def _serialize_credentials(credentials: RouterCredential) -> RouterCredentialsResponse:
    return RouterCredentialsResponse(
        router_id=str(credentials.router_id),
        api_username=credentials.api_username,
        api_port=int(credentials.api_port),
        use_ssl=bool(credentials.use_ssl),
        connection_status=credentials.connection_status,
        last_connected_at=credentials.last_connected_at,
    )


async def _ensure_router_in_tenant(db: AsyncSession, router_id: uuid.UUID, isp_operator_id: uuid.UUID) -> Router:
    router_row = (
        await db.execute(select(Router).where(Router.id == router_id, Router.isp_operator_id == isp_operator_id))
    ).scalar_one_or_none()
    if router_row is None:
        raise HTTPException(status_code=404, detail="Router not found")
    return router_row


def _serialize_template(template: ConfigTemplate) -> ConfigTemplateResponse:
    return ConfigTemplateResponse(
        id=str(template.id),
        name=template.name,
        description=template.description,
        template_data=template.template_data,
        is_default=bool(template.is_default),
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def _serialize_metric(row: RouterMetric) -> RouterMetricResponse:
    return RouterMetricResponse(
        id=str(row.id),
        router_id=str(row.router_id),
        collected_at=row.collected_at,
        cpu_load_percent=row.cpu_load_percent,
        memory_used_percent=row.memory_used_percent,
        uptime_seconds=row.uptime_seconds,
        active_sessions=row.active_sessions,
        total_tx_bytes=row.total_tx_bytes,
        total_rx_bytes=row.total_rx_bytes,
        board_name=row.board_name,
        ros_version=row.ros_version,
    )


def _serialize_provision_status(log_row: RouterProvisionLog) -> ProvisionStatusResponse:
    commands = list(log_row.commands_executed or [])
    progress = None
    if commands:
        latest = commands[-1]
        progress = latest.get("message") or latest.get("command") or latest.get("step")
    return ProvisionStatusResponse(
        log_id=str(log_row.id),
        router_id=str(log_row.router_id),
        status=log_row.status,
        progress=progress,
        error_message=log_row.error_message,
        commands_executed=commands,
        started_at=log_row.started_at,
        completed_at=log_row.completed_at,
    )


_verified_connections: dict[str, datetime] = {}


def _verification_key(host: str, port: int, username: str) -> str:
    return f"{host}|{port}|{username}".lower()


def _remember_verified_connection(host: str, port: int, username: str) -> None:
    _verified_connections[_verification_key(host, port, username)] = datetime.now(timezone.utc) + timedelta(minutes=15)


def _get_verified_connection(host: str, port: int, username: str) -> bool:
    key = _verification_key(host, port, username)
    expires_at = _verified_connections.get(key)
    if expires_at is None:
        return False
    if expires_at < datetime.now(timezone.utc):
        _verified_connections.pop(key, None)
        return False
    return True
