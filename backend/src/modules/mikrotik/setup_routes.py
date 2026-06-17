"""Router setup wizard endpoints — UI-driven full router configuration.

All endpoints are admin-scoped and tenant-aware. Each ``apply`` is atomic per
section and writes a row to ``router_provision_log`` (constraint #2/#3). The
RADIUS shared secret is decrypted server-side and sent straight to MikroTik; it
is never returned to the client (constraint #1).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.base import get_db
from src.db.models import Router, RouterProvisionLog, RouterSetupStatus
from src.middleware.auth import TenantContext, get_admin_tenant_context
from src.modules.mikrotik.api_service import MikroTikOperationError, RouterCredentialsMissingError
from src.modules.mikrotik import setup_service as svc
from src.modules.mikrotik import setup_status as store
from src.modules.mikrotik.radius_host import resolve_radius_host
from src.modules.mikrotik.setup_types import (
    ApplyResultResponse,
    HotspotApplyRequest,
    NasSecretResponse,
    NatApplyRequest,
    NetworkApplyRequest,
    RadiusApplyRequest,
    SectionStatus,
    SetupStatusResponse,
)
from src.utils.encryption import decrypt_secret
from src.utils.portal_token import create_portal_router_token

router = APIRouter(prefix="/admin", tags=["router-setup"])
setup = svc.RouterSetupService()
settings = get_settings()

OFFLINE_MESSAGE = "Router must be connected via VPN tunnel or direct IP to apply settings"


# ─── Helpers ────────────────────────────────────────────────────────────────


async def _load_router(db: AsyncSession, router_id: uuid.UUID, tenant: TenantContext) -> Router:
    row = (
        await db.execute(select(Router).where(Router.id == router_id, Router.isp_operator_id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Router not found")
    return row


def _is_online(router: Router) -> bool:
    return bool(router.is_online) or bool(router.wg_enabled and router.wg_is_connected)


def _router_secret(router_row: Router) -> str:
    """Return the router's RADIUS shared secret for server-side use only.

    ``nas_secret`` is always stored encrypted (create/update encrypt it and
    migration 016 re-encrypted any legacy plaintext rows), so a plain
    ``decrypt_secret`` is sufficient. The value is never sent to the client."""
    return decrypt_secret(router_row.nas_secret)


async def _log_apply(
    db: AsyncSession,
    router_id: uuid.UUID,
    tenant: TenantContext,
    action: str,
    status: str,
    commands: list[dict],
    error: str | None = None,
) -> None:
    db.add(
        RouterProvisionLog(
            router_id=router_id,
            triggered_by=str(tenant.user_id),
            action=action,
            status=status,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            error_message=error,
            commands_executed=commands or [],
        )
    )
    await db.commit()


async def _run_apply(db, router_id, tenant, section, action, operation_coro, *, config_to_store, terminal_commands):
    """Shared apply flow: offline guard → run → log → persist status → respond."""
    try:
        message, commands = await operation_coro
    except RouterCredentialsMissingError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except MikroTikOperationError as exc:
        await _log_apply(db, router_id, tenant, action, "failed", exc.commands, str(exc))
        await store.record_apply(db, router_id, section, "error", {"error": str(exc)})
        raise HTTPException(status_code=422, detail=str(exc))

    await _log_apply(db, router_id, tenant, action, "success", commands)
    row = await store.record_apply(db, router_id, section, "configured", config_to_store)
    applied_at = getattr(row, f"{section}_applied_at")
    return ApplyResultResponse(
        success=True,
        message=message,
        status="configured",
        last_applied_at=applied_at,
        commands_executed=commands,
        terminal_commands=terminal_commands,
    )


# ─── Combined status ────────────────────────────────────────────────────────


@router.get("/routers/{router_id}/setup/status", response_model=SetupStatusResponse)
async def setup_status_endpoint(
    router_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    router_row = await _load_router(db, router_id, tenant)
    row = (
        await db.execute(select(RouterSetupStatus).where(RouterSetupStatus.router_id == router_id))
    ).scalar_one_or_none()
    return SetupStatusResponse(
        router_id=str(router_id),
        online=_is_online(router_row),
        sections_complete=store.sections_complete(row),
        network=SectionStatus(**store.section_payload(row, "network")),
        hotspot=SectionStatus(**store.section_payload(row, "hotspot")),
        radius=SectionStatus(**store.section_payload(row, "radius")),
        nat=SectionStatus(**store.section_payload(row, "nat")),
    )


# ─── Section 1: Network ─────────────────────────────────────────────────────


@router.get("/routers/{router_id}/setup/network/detect")
async def detect_network(router_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    await _load_router(db, router_id, tenant)
    try:
        detected, _ = await setup.detect_network(str(router_id))
    except (RouterCredentialsMissingError, MikroTikOperationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    status = svc.derive_network_status(detected)
    await store.record_detect(db, router_id, "network", status, {"detected": detected})
    return {"status": status, "detected": detected}


@router.post("/routers/{router_id}/setup/network/apply", response_model=ApplyResultResponse)
async def apply_network(router_id: uuid.UUID, body: NetworkApplyRequest, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    router_row = await _load_router(db, router_id, tenant)
    if not _is_online(router_row):
        raise HTTPException(status_code=409, detail=OFFLINE_MESSAGE)
    try:
        plan = svc.plan_subnet(body.gateway_ip, body.prefix)
        pool_start = body.pool_start or plan.pool_start
        pool_end = body.pool_end or plan.pool_end
        pool_start, pool_end = svc.validate_pool_range(plan, pool_start, pool_end)
    except svc.SetupValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    data = {
        "bridge_name": body.bridge_name,
        "interfaces": body.interfaces,
        "gateway_ip": plan.gateway_ip,
        "prefix": plan.prefix,
        "pool_start": pool_start,
        "pool_end": pool_end,
        "network_address": plan.network_address,
        "dns": body.dns,
        "lease_time": body.lease_time,
        "pool_name": svc.POOL_NAME,
    }
    config_to_store = {**data, "hotspot_network": plan.hotspot_network}
    return await _run_apply(
        db, router_id, tenant, "network", "setup_network",
        setup.apply_network(str(router_id), data),
        config_to_store=config_to_store,
        terminal_commands=svc.network_terminal_commands(data),
    )


# ─── Section 2: Hotspot ─────────────────────────────────────────────────────


@router.get("/routers/{router_id}/setup/hotspot/detect")
async def detect_hotspot(router_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    await _load_router(db, router_id, tenant)
    try:
        detected, _ = await setup.detect_hotspot(str(router_id))
    except (RouterCredentialsMissingError, MikroTikOperationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    status = svc.derive_hotspot_status(detected)
    await store.record_detect(db, router_id, "hotspot", status, {"detected": detected})
    return {"status": status, "detected": detected}


@router.post("/routers/{router_id}/setup/hotspot/apply", response_model=ApplyResultResponse)
async def apply_hotspot(router_id: uuid.UUID, body: HotspotApplyRequest, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    router_row = await _load_router(db, router_id, tenant)
    if not _is_online(router_row):
        raise HTTPException(status_code=409, detail=OFFLINE_MESSAGE)

    # Section 1 must be applied first — the hotspot needs the bridge IP + pool.
    setup_row = (await db.execute(select(RouterSetupStatus).where(RouterSetupStatus.router_id == router_id))).scalar_one_or_none()
    if setup_row is None or setup_row.network_status != "configured":
        raise HTTPException(status_code=409, detail="Network setup must be completed before configuring the hotspot.")

    data = {
        "bridge_name": body.bridge_name,
        "dns_name": body.dns_name,
        "login_by": body.login_by if "http-pap" in body.login_by else ["http-pap", *body.login_by],
        "session_timeout": body.session_timeout,
        "idle_timeout": body.idle_timeout,
        "addresses_per_mac": body.addresses_per_mac,
        "pool_name": svc.POOL_NAME,
    }
    return await _run_apply(
        db, router_id, tenant, "hotspot", "setup_hotspot",
        setup.apply_hotspot(str(router_id), data),
        config_to_store=data,
        terminal_commands=svc.hotspot_terminal_commands(data),
    )


# ─── Section 3: RADIUS ──────────────────────────────────────────────────────


@router.get("/routers/{router_id}/setup/radius/detect")
async def detect_radius(router_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    router_row = await _load_router(db, router_id, tenant)
    try:
        detected, _ = await setup.detect_radius(str(router_id))
    except (RouterCredentialsMissingError, MikroTikOperationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    radius_host = resolve_radius_host(router_row, settings)
    status = svc.derive_radius_status(detected, radius_host)
    # Warn if an entry points somewhere other than the platform host.
    other = [e.get("address") for e in detected.get("entries", []) if e.get("address") and e.get("address") != radius_host]
    await store.record_detect(db, router_id, "radius", status, {"detected": detected})
    return {"status": status, "detected": detected, "radius_host": radius_host, "other_addresses": other}


@router.post("/routers/{router_id}/setup/radius/apply", response_model=ApplyResultResponse)
async def apply_radius(router_id: uuid.UUID, body: RadiusApplyRequest, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    router_row = await _load_router(db, router_id, tenant)
    if not _is_online(router_row):
        raise HTTPException(status_code=409, detail=OFFLINE_MESSAGE)
    # Tunnel-only platform: a router reached over WireGuard must point its RADIUS
    # client at the server's tunnel IP, not RADIUS_PUBLIC_HOST (unreachable from
    # the tunnel). resolve_radius_host() falls back to RADIUS_PUBLIC_HOST only for
    # a (currently unreachable) non-tunneled router.
    radius_host = resolve_radius_host(router_row, settings)
    if not radius_host:
        raise HTTPException(status_code=422, detail="Platform RADIUS host is not configured (set WG_SERVER_TUNNEL_IP or RADIUS_PUBLIC_HOST)")

    # Decrypt the router's unique shared secret server-side — never sent to client.
    secret = _router_secret(router_row)
    data = {
        "radius_host": radius_host,
        "radius_secret": secret,
        "auth_port": 1812,
        "accounting_port": 1813,
        "service": "hotspot",
        "timeout": body.timeout,
    }
    # Stored config deliberately omits the secret.
    config_to_store = {"radius_host": radius_host, "auth_port": 1812, "accounting_port": 1813, "service": "hotspot", "timeout": body.timeout}
    terminal = svc.radius_terminal_commands({"radius_host": radius_host, "auth_port": 1812, "accounting_port": 1813, "service": "hotspot", "timeout": body.timeout})
    return await _run_apply(
        db, router_id, tenant, "radius", "setup_radius",
        setup.apply_radius(str(router_id), data),
        config_to_store=config_to_store,
        terminal_commands=terminal,
    )


@router.get("/routers/{router_id}/nas-secret", response_model=NasSecretResponse)
async def nas_secret(router_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    router_row = await _load_router(db, router_id, tenant)
    secret = _router_secret(router_row)
    tail = secret[-3:] if len(secret) >= 3 else secret
    return NasSecretResponse(masked="●" * 8, hint=f"ends in …{tail}")


# ─── Section 4: NAT & firewall ──────────────────────────────────────────────


@router.get("/routers/{router_id}/setup/nat/detect")
async def detect_nat(router_id: uuid.UUID, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    await _load_router(db, router_id, tenant)
    try:
        detected, _ = await setup.detect_nat(str(router_id))
    except (RouterCredentialsMissingError, MikroTikOperationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    status = svc.derive_nat_status(detected)
    await store.record_detect(db, router_id, "nat", status, {"detected": detected})
    return {"status": status, "detected": detected, "duplicate_masquerade": detected.get("masquerade_count", 0) > 1}


@router.post("/routers/{router_id}/setup/nat/apply", response_model=ApplyResultResponse)
async def apply_nat(router_id: uuid.UUID, body: NatApplyRequest, db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    router_row = await _load_router(db, router_id, tenant)
    if not _is_online(router_row):
        raise HTTPException(status_code=409, detail=OFFLINE_MESSAGE)

    hotspot_network = body.hotspot_network
    if not hotspot_network:
        setup_row = (await db.execute(select(RouterSetupStatus).where(RouterSetupStatus.router_id == router_id))).scalar_one_or_none()
        if setup_row:
            hotspot_network = svc.resolve_hotspot_network(setup_row.network_config)
    if not hotspot_network:
        raise HTTPException(status_code=422, detail="hotspot_network is required (complete Network setup first or provide it explicitly)")

    if body.remove_duplicates:
        try:
            await setup.remove_duplicate_nat(str(router_id))
        except MikroTikOperationError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    data = {
        "wan_interface": body.wan_interface,
        "hotspot_network": hotspot_network,
        "enable_nat": body.enable_nat,
        "firewall_options": body.firewall_options,
    }

    async def _apply_nat_then_portal():
        """NAT is the final wizard step, so we also wire the captive-portal
        redirect here: once the router has internet (NAT masquerade), it can fetch
        the platform login page. Failures surface as a NAT-section error so the
        operator knows the portal didn't get configured."""
        message, commands = await setup.apply_nat(str(router_id), data)
        portal_base = get_settings().effective_portal_public_base_url
        if portal_base:
            token = create_portal_router_token(str(router_id))
            portal_message, portal_commands = await setup.apply_portal(str(router_id), portal_base, token)
            return f"{message}; {portal_message}", commands + portal_commands
        return f"{message}; portal redirect skipped (portal base URL not configured)", commands

    return await _run_apply(
        db, router_id, tenant, "nat", "setup_nat",
        _apply_nat_then_portal(),
        config_to_store=data,
        terminal_commands=svc.nat_terminal_commands(data),
    )
