from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from src.config import get_settings
from src.modules.mikrotik.api_service import MikroTikAPIService
from src.modules.mikrotik.radius_host import resolve_radius_host
from src.modules.mikrotik.types import DiagnosticCheck, DiagnosticsResponse

settings = get_settings()


class MikroTikDiagnosticsService:
    def __init__(self) -> None:
        self.api_service = MikroTikAPIService()

    async def run(self, router_id: str) -> DiagnosticsResponse:
        access = None
        access_error = None
        try:
            access = await self.api_service._load_router_access(router_id)
        except Exception as exc:
            access_error = str(exc)

        async def gather_checks() -> list[DiagnosticCheck]:
            checks = await asyncio.gather(
                self._api_connectivity(router_id, access, access_error),
                self._system_resources(router_id, access, access_error),
                self._radius_config(router_id, access, access_error),
                self._hotspot_status(router_id, access, access_error),
                self._active_sessions(router_id, access, access_error),
                self._interface_health(router_id, access, access_error),
            )
            return checks

        try:
            checks = await asyncio.wait_for(gather_checks(), timeout=12)
        except asyncio.TimeoutError:
            checks = [
                DiagnosticCheck(name=name, status="fail", message="Diagnostics timed out", data={})
                for name in (
                    "api_connectivity",
                    "system_resources",
                    "radius_config",
                    "hotspot_status",
                    "active_sessions",
                    "interface_health",
                )
            ]
        statuses = {check.status for check in checks}
        overall_status = "critical" if "fail" in statuses else "warning" if "warn" in statuses else "healthy"
        return DiagnosticsResponse(
            router_id=router_id,
            collected_at=datetime.now(timezone.utc),
            overall_status=overall_status,
            checks=checks,
        )

    async def _api_connectivity(self, router_id: str, access, access_error: str | None) -> DiagnosticCheck:
        try:
            if access is None:
                raise RuntimeError(access_error or "Router credentials unavailable")
            info, _ = await self.api_service._run_direct_operation(
                host=access.host,
                port=access.api_port,
                username=access.api_username,
                password=access.api_password,
                use_ssl=access.use_ssl,
                operation=lambda runner: self.api_service._sync_get_system_info(runner, {}),
                timeout=self.api_service.command_timeout,
            )
            return DiagnosticCheck(
                name="api_connectivity",
                status="pass",
                message=f"Connected - RouterOS {info.ros_version or 'unknown'}",
                data=info.model_dump(),
            )
        except Exception as exc:
            return DiagnosticCheck(name="api_connectivity", status="fail", message=str(exc), data={})

    async def _system_resources(self, router_id: str, access, access_error: str | None) -> DiagnosticCheck:
        try:
            if access is None:
                raise RuntimeError(access_error or "Router credentials unavailable")
            info, _ = await self.api_service._run_direct_operation(
                host=access.host,
                port=access.api_port,
                username=access.api_username,
                password=access.api_password,
                use_ssl=access.use_ssl,
                operation=lambda runner: self.api_service._sync_get_system_info(runner, {}),
                timeout=self.api_service.command_timeout,
            )
            return DiagnosticCheck(
                name="system_resources",
                status="pass",
                message="System resources retrieved",
                data=info.model_dump(),
            )
        except Exception as exc:
            return DiagnosticCheck(name="system_resources", status="fail", message=str(exc), data={})

    async def _radius_config(self, router_id: str, access, access_error: str | None) -> DiagnosticCheck:
        try:
            if access is None:
                raise RuntimeError(access_error or "Router credentials unavailable")
            configs, _ = await self.api_service._run_direct_operation(
                host=access.host,
                port=access.api_port,
                username=access.api_username,
                password=access.api_password,
                use_ssl=access.use_ssl,
                operation=lambda runner: self.api_service._sync_get_radius_config(runner, {}),
                timeout=self.api_service.command_timeout,
            )
            expected_host = resolve_radius_host(access, settings)
            correct = any(cfg.address == expected_host for cfg in configs)
            if not configs:
                return DiagnosticCheck(name="radius_config", status="fail", message="No RADIUS entries configured", data={})
            if not correct:
                return DiagnosticCheck(
                    name="radius_config",
                    status="fail",
                    message=f"RADIUS entry does not point to {expected_host}",
                    data={"radius_entries": [cfg.model_dump() for cfg in configs]},
                )
            return DiagnosticCheck(
                name="radius_config",
                status="pass",
                message="RADIUS configuration is present",
                data={"radius_entries": [cfg.model_dump() for cfg in configs]},
            )
        except Exception as exc:
            return DiagnosticCheck(name="radius_config", status="fail", message=str(exc), data={})

    async def _hotspot_status(self, router_id: str, access, access_error: str | None) -> DiagnosticCheck:
        try:
            if access is None:
                raise RuntimeError(access_error or "Router credentials unavailable")
            servers_task = self.api_service._run_direct_operation(
                host=access.host,
                port=access.api_port,
                username=access.api_username,
                password=access.api_password,
                use_ssl=access.use_ssl,
                operation=lambda runner: self.api_service._sync_get_hotspot_servers(runner, {}),
                timeout=self.api_service.command_timeout,
            )
            radius_task = self.api_service._run_direct_operation(
                host=access.host,
                port=access.api_port,
                username=access.api_username,
                password=access.api_password,
                use_ssl=access.use_ssl,
                operation=lambda runner: self.api_service._sync_get_radius_config(runner, {}),
                timeout=self.api_service.command_timeout,
            )
            servers, radius = await asyncio.gather(servers_task, radius_task)
            servers = servers[0]
            radius = radius[0]
            if not servers:
                return DiagnosticCheck(name="hotspot_status", status="fail", message="No hotspot server configured", data={})
            radius_enabled = any((cfg.service or "").find("hotspot") >= 0 for cfg in radius)
            if not radius_enabled:
                return DiagnosticCheck(name="hotspot_status", status="fail", message="Hotspot RADIUS is not enabled", data={})
            return DiagnosticCheck(
                name="hotspot_status",
                status="pass",
                message="Hotspot is running with RADIUS",
                data={"servers": [server.model_dump() for server in servers]},
            )
        except Exception as exc:
            return DiagnosticCheck(name="hotspot_status", status="fail", message=str(exc), data={})

    async def _active_sessions(self, router_id: str, access, access_error: str | None) -> DiagnosticCheck:
        try:
            if access is None:
                raise RuntimeError(access_error or "Router credentials unavailable")
            sessions, _ = await self.api_service._run_direct_operation(
                host=access.host,
                port=access.api_port,
                username=access.api_username,
                password=access.api_password,
                use_ssl=access.use_ssl,
                operation=lambda runner: self.api_service._sync_get_active_users(runner, {}),
                timeout=self.api_service.command_timeout,
            )
            return DiagnosticCheck(
                name="active_sessions",
                status="pass",
                message=f"{len(sessions)} active session(s)",
                data={"count": len(sessions)},
            )
        except Exception as exc:
            return DiagnosticCheck(name="active_sessions", status="fail", message=str(exc), data={})

    async def _interface_health(self, router_id: str, access, access_error: str | None) -> DiagnosticCheck:
        try:
            if access is None:
                raise RuntimeError(access_error or "Router credentials unavailable")
            interfaces, _ = await self.api_service._run_direct_operation(
                host=access.host,
                port=access.api_port,
                username=access.api_username,
                password=access.api_password,
                use_ssl=access.use_ssl,
                operation=lambda runner: self.api_service._sync_get_interfaces(runner, {}),
                timeout=self.api_service.command_timeout,
            )
            running = [iface for iface in interfaces if iface.running and not iface.disabled]
            if not running:
                return DiagnosticCheck(name="interface_health", status="fail", message="No running interfaces detected", data={})
            return DiagnosticCheck(
                name="interface_health",
                status="pass",
                message=f"{len(running)} interface(s) are up",
                data={"interfaces": [iface.model_dump() for iface in interfaces]},
            )
        except Exception as exc:
            return DiagnosticCheck(name="interface_health", status="fail", message=str(exc), data={})
