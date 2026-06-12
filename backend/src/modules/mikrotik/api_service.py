from __future__ import annotations

import asyncio
import ipaddress
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

import routeros_api
from routeros_api.exceptions import RouterOsApiError
from sqlalchemy import select

from src.config import get_settings
from src.db.base import async_session_factory
from src.db.models import Router, RouterCredential
from src.modules.mikrotik.types import (
    ActiveUserInfo,
    ConnectionTestResult,
    HotspotServerInfo,
    InterfaceInfo,
    ProvisionResult,
    RadiusConfig,
    SystemInfo,
)
from src.utils.encryption import decrypt_secret

settings = get_settings()


class RouterCredentialsMissingError(Exception):
    pass


class MikroTikOperationError(Exception):
    def __init__(self, message: str, *, commands: list[dict[str, Any]] | None = None, status: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.commands = commands or []
        self.status = status


@dataclass
class RouterAccess:
    router_id: str
    host: str
    api_port: int
    api_username: str
    api_password: str
    use_ssl: bool
    nas_secret: str


@dataclass
class MikroTikConnection:
    pool: routeros_api.RouterOsApiPool
    api: Any


@dataclass
class SyncCommandRunner:
    api: Any
    commands: list[dict[str, Any]] = field(default_factory=list)

    def execute(
        self,
        path: str,
        command: str = "print",
        *,
        params: dict[str, Any] | None = None,
        queries: dict[str, Any] | None = None,
    ) -> Any:
        encoded_params = _encode_mapping_for_routeros(params or {})
        encoded_queries = _encode_mapping_for_routeros(queries or {})
        entry = {
            "path": path,
            "command": command,
            "params": _sanitize_mapping(params or {}),
            "queries": _sanitize_mapping(queries or {}),
            "status": "running",
            "started_at": _utcnow().isoformat(),
        }
        self.commands.append(entry)
        try:
            resource = self.api.get_resource(path)
            if command == "print":
                response = resource.call("print", arguments=encoded_params, queries=encoded_queries)
            else:
                response = resource.call(command, arguments=encoded_params, queries=encoded_queries)
            entry["status"] = "success"
            entry["completed_at"] = _utcnow().isoformat()
            entry["response"] = _sanitize_value(response)
            return response
        except Exception as exc:  # pragma: no cover - exercised through callers
            entry["status"] = "failed"
            entry["completed_at"] = _utcnow().isoformat()
            entry["error"] = _normalize_error(exc)
            raise MikroTikOperationError(entry["error"], commands=self.commands, status=_classify_status(entry["error"])) from exc


class MikroTikAPIService:
    def __init__(self) -> None:
        self.connection_timeout = settings.mikrotik_api_timeout
        self.command_timeout = settings.mikrotik_cmd_timeout

    async def _run_sync(self, func, *args, timeout: int | None = None):
        loop = asyncio.get_running_loop()
        call = loop.run_in_executor(None, func, *args)
        return await asyncio.wait_for(call, timeout=timeout or self.command_timeout)

    def connect(
        self,
        router_id: str | None = None,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool = False,
    ) -> MikroTikConnection:
        pool = routeros_api.RouterOsApiPool(
            host=host,
            username=username,
            password=password,
            port=port,
            plaintext_login=True,
            use_ssl=use_ssl,
            ssl_verify=False,
            ssl_verify_hostname=False,
        )
        pool.socket_timeout = self.connection_timeout
        api = pool.get_api()
        pool.set_timeout(self.command_timeout)
        return MikroTikConnection(pool=pool, api=api)

    def disconnect(self, connection: MikroTikConnection) -> None:
        connection.pool.disconnect()

    async def test_connection_async(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool = False,
    ) -> ConnectionTestResult:
        try:
            return await self._run_sync(
                self.test_connection,
                host,
                port,
                username,
                password,
                use_ssl,
                timeout=self.connection_timeout + 2,
            )
        except asyncio.TimeoutError:
            return ConnectionTestResult(success=False, board_name=None, ros_version=None, error="Connection timed out")

    def test_connection(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool = False,
    ) -> ConnectionTestResult:
        connection: MikroTikConnection | None = None
        try:
            connection = self.connect(host=host, port=port, username=username, password=password, use_ssl=use_ssl)
            rows = connection.api.get_resource("/system/resource").call("print")
            payload = rows[0] if rows else {}
            return ConnectionTestResult(
                success=True,
                board_name=payload.get("board-name"),
                ros_version=payload.get("version"),
                error=None,
            )
        except (RouterOsApiError, ConnectionError, OSError) as exc:
            return ConnectionTestResult(success=False, board_name=None, ros_version=None, error=_normalize_error(exc))
        finally:
            if connection is not None:
                try:
                    self.disconnect(connection)
                except Exception:
                    pass

    async def list_interfaces_temp(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool = False,
    ) -> list[InterfaceInfo]:
        def operation(runner: SyncCommandRunner):
            rows = runner.execute("/interface", "print")
            return [self._map_interface(row) for row in rows]

        result, _ = await self._run_direct_operation(
            host=host,
            port=port,
            username=username,
            password=password,
            use_ssl=use_ssl,
            operation=operation,
            timeout=self.command_timeout,
        )
        return result

    async def get_system_info(self, router_id: str) -> SystemInfo:
        result, _ = await self._run_router_operation(router_id, self._sync_get_system_info)
        return result

    async def get_identity(self, router_id: str) -> str:
        result, _ = await self._run_router_operation(router_id, self._sync_get_identity)
        return result

    async def get_interfaces(self, router_id: str) -> list[InterfaceInfo]:
        result, _ = await self._run_router_operation(router_id, self._sync_get_interfaces)
        return result

    async def get_hotspot_servers(self, router_id: str) -> list[HotspotServerInfo]:
        result, _ = await self._run_router_operation(router_id, self._sync_get_hotspot_servers)
        return result

    async def get_active_hotspot_users(self, router_id: str) -> list[ActiveUserInfo]:
        result, _ = await self._run_router_operation(router_id, self._sync_get_active_users)
        return result

    async def get_radius_config(self, router_id: str) -> list[RadiusConfig]:
        result, _ = await self._run_router_operation(router_id, self._sync_get_radius_config)
        return result

    async def set_radius_server(
        self,
        router_id: str,
        radius_host: str,
        radius_secret: str,
        auth_port: int = 1812,
        accounting_port: int = 1813,
        service: str = "hotspot",
    ) -> ProvisionResult:
        data = {
            "radius_host": radius_host,
            "radius_secret": radius_secret,
            "auth_port": auth_port,
            "accounting_port": accounting_port,
            "service": service,
        }
        result, commands = await self._run_router_operation(router_id, self._sync_set_radius_server, data=data)
        return ProvisionResult(success=True, message=result, commands_executed=commands)

    async def enable_hotspot_radius(self, router_id: str, hotspot_server_name: str = "hotspot1") -> ProvisionResult:
        result, commands = await self._run_router_operation(
            router_id,
            self._sync_enable_hotspot_radius,
            data={"hotspot_server_name": hotspot_server_name},
        )
        return ProvisionResult(success=True, message=result, commands_executed=commands)

    async def set_hotspot_dns_name(self, router_id: str, dns_name: str) -> ProvisionResult:
        result, commands = await self._run_router_operation(
            router_id,
            self._sync_set_hotspot_dns_name,
            data={"dns_name": dns_name},
        )
        return ProvisionResult(success=True, message=result, commands_executed=commands)

    async def configure_external_portal(self, router_id: str, portal_base_url: str) -> ProvisionResult:
        result, commands = await self._run_router_operation(
            router_id,
            self._sync_configure_external_portal,
            data={"portal_base_url": portal_base_url},
            timeout=max(self.command_timeout, self.connection_timeout + 10),
        )
        return ProvisionResult(success=True, message=result, commands_executed=commands)

    async def execute_template_commands(
        self,
        router_id: str,
        commands_to_run: list[dict[str, Any]],
    ) -> ProvisionResult:
        timeout = max(self.command_timeout, len(commands_to_run) * self.command_timeout)
        result, commands = await self._run_router_operation(
            router_id,
            self._sync_execute_template_commands,
            data={"commands": commands_to_run},
            timeout=timeout,
        )
        return ProvisionResult(success=True, message=result, commands_executed=commands)

    async def verify_radius_host(self, router_id: str, radius_host: str) -> ProvisionResult:
        result, commands = await self._run_router_operation(
            router_id,
            self._sync_verify_radius_host,
            data={"radius_host": radius_host},
        )
        return ProvisionResult(success=True, message=result, commands_executed=commands)

    async def disconnect_hotspot_user(self, router_id: str, active_id: str) -> ProvisionResult:
        result, commands = await self._run_router_operation(
            router_id,
            self._sync_disconnect_hotspot_user,
            data={"active_id": active_id},
        )
        return ProvisionResult(success=True, message=result, commands_executed=commands)

    async def reboot_router(self, router_id: str) -> ProvisionResult:
        result, commands = await self._run_router_operation(router_id, self._sync_reboot_router)
        return ProvisionResult(success=True, message=result, commands_executed=commands)

    async def run_operation(
        self,
        router_id: str,
        operation: Callable[[SyncCommandRunner, dict[str, Any]], Any],
        *,
        data: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Public entry point for running an arbitrary sync operation against a
        router over its preferred connection (WireGuard tunnel or direct IP).
        Returns (operation_result, executed_commands). Used by the setup wizard
        service so it does not have to re-implement the connection plumbing."""
        return await self._run_router_operation(router_id, operation, data=data, timeout=timeout)

    async def _run_router_operation(
        self,
        router_id: str,
        operation: Callable[[SyncCommandRunner, dict[str, Any]], Any],
        *,
        data: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> tuple[Any, list[dict[str, Any]]]:
        access = await self._load_router_access(router_id)
        try:
            result, commands = await self._run_sync(
                self._execute_connected_operation,
                access,
                operation,
                data or {},
                timeout=timeout or self.command_timeout,
            )
            await self._update_router_connection_status(router_id, "online")
            return result, commands
        except asyncio.TimeoutError as exc:
            message = "Connection timed out"
            await self._update_router_connection_status(router_id, "timeout")
            raise MikroTikOperationError(message, commands=[], status="timeout") from exc
        except MikroTikOperationError as exc:
            await self._update_router_connection_status(router_id, exc.status or _classify_status(exc.message))
            raise

    async def _run_direct_operation(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool,
        operation: Callable[[SyncCommandRunner], Any],
        timeout: int,
    ) -> tuple[Any, list[dict[str, Any]]]:
        access = RouterAccess(
            router_id="temporary",
            host=host,
            api_port=port,
            api_username=username,
            api_password=password,
            use_ssl=use_ssl,
            nas_secret="",
        )
        try:
            return await self._run_sync(self._execute_connected_operation_direct, access, operation, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise MikroTikOperationError("Connection timed out", commands=[], status="timeout") from exc

    async def _load_router_access(self, router_id: str) -> RouterAccess:
        async with async_session_factory() as db:
            result = await db.execute(
                select(Router, RouterCredential)
                .join(RouterCredential, RouterCredential.router_id == Router.id)
                .where(Router.id == router_id)
            )
            row = result.one_or_none()
            if row is None:
                raise RouterCredentialsMissingError("Router credentials are required before provisioning")
            router, credentials = row
            # Prefer the WireGuard tunnel IP when the tunnel is up — the router
            # may be behind NAT and unreachable at its direct IP. The direct IP
            # is optional (nullable), so a router with neither tunnel nor IP has
            # no reachable address.
            if router.wg_enabled and router.wg_is_connected and router.wg_tunnel_ip:
                host = str(router.wg_tunnel_ip)
            elif router.ip_address:
                host = str(router.ip_address)
            else:
                raise RouterCredentialsMissingError(
                    "Router has no reachable address — set up a VPN tunnel or a direct IP first"
                )
            return RouterAccess(
                router_id=str(router.id),
                host=host,
                api_port=int(credentials.api_port),
                api_username=credentials.api_username,
                api_password=decrypt_secret(credentials.api_password_encrypted),
                use_ssl=bool(credentials.use_ssl),
                nas_secret=decrypt_secret(router.nas_secret),
            )

    async def _update_router_connection_status(self, router_id: str, status: str) -> None:
        now = _utcnow()
        async with async_session_factory() as db:
            result = await db.execute(select(Router, RouterCredential).join(RouterCredential).where(Router.id == router_id))
            row = result.one_or_none()
            if row is None:
                return
            router, credentials = row
            credentials.connection_status = status
            credentials.last_connected_at = now
            router.is_online = status == "online"
            if status == "online":
                router.last_seen_at = now
            await db.commit()

    def _execute_connected_operation(
        self,
        access: RouterAccess,
        operation: Callable[[SyncCommandRunner, dict[str, Any]], Any],
        data: dict[str, Any],
    ) -> tuple[Any, list[dict[str, Any]]]:
        connection: MikroTikConnection | None = None
        connect_entry = {
            "path": "connect",
            "command": "open",
            "params": {"host": access.host, "port": access.api_port, "username": access.api_username, "use_ssl": access.use_ssl},
            "status": "running",
            "started_at": _utcnow().isoformat(),
        }
        commands = [connect_entry]
        try:
            connection = self.connect(
                router_id=access.router_id,
                host=access.host,
                port=access.api_port,
                username=access.api_username,
                password=access.api_password,
                use_ssl=access.use_ssl,
            )
            connect_entry["status"] = "success"
            connect_entry["completed_at"] = _utcnow().isoformat()
            runner = SyncCommandRunner(connection.api, commands)
            result = operation(runner, data)
            return result, runner.commands
        except MikroTikOperationError as exc:
            if "completed_at" not in connect_entry:
                connect_entry["status"] = "failed"
                connect_entry["completed_at"] = _utcnow().isoformat()
                connect_entry["error"] = exc.message
            raise MikroTikOperationError(exc.message, commands=commands if len(commands) > len(exc.commands) else exc.commands, status=exc.status)
        except (RouterOsApiError, ConnectionError, OSError) as exc:
            connect_entry["status"] = "failed"
            connect_entry["completed_at"] = _utcnow().isoformat()
            connect_entry["error"] = _normalize_error(exc)
            raise MikroTikOperationError(connect_entry["error"], commands=commands, status=_classify_status(connect_entry["error"])) from exc
        finally:
            if connection is not None:
                try:
                    self.disconnect(connection)
                except Exception:
                    pass

    def _execute_connected_operation_direct(
        self,
        access: RouterAccess,
        operation: Callable[[SyncCommandRunner], Any],
    ) -> tuple[Any, list[dict[str, Any]]]:
        connection: MikroTikConnection | None = None
        commands: list[dict[str, Any]] = []
        try:
            connection = self.connect(
                host=access.host,
                port=access.api_port,
                username=access.api_username,
                password=access.api_password,
                use_ssl=access.use_ssl,
            )
            runner = SyncCommandRunner(connection.api, commands)
            result = operation(runner)
            return result, runner.commands
        except (RouterOsApiError, ConnectionError, OSError) as exc:
            raise MikroTikOperationError(_normalize_error(exc), commands=commands, status=_classify_status(_normalize_error(exc))) from exc
        finally:
            if connection is not None:
                try:
                    self.disconnect(connection)
                except Exception:
                    pass

    def _sync_get_system_info(self, runner: SyncCommandRunner, _: dict[str, Any]) -> SystemInfo:
        rows = runner.execute("/system/resource", "print")
        payload = rows[0] if rows else {}
        return SystemInfo(
            board_name=payload.get("board-name"),
            ros_version=payload.get("version"),
            cpu_load=_to_int(payload.get("cpu-load")),
            free_memory=_to_int(payload.get("free-memory")),
            total_memory=_to_int(payload.get("total-memory")),
            uptime=payload.get("uptime"),
            uptime_seconds=_parse_uptime_seconds(payload.get("uptime")),
            architecture_name=payload.get("architecture-name"),
        )

    def _sync_get_identity(self, runner: SyncCommandRunner, _: dict[str, Any]) -> str:
        rows = runner.execute("/system/identity", "print")
        payload = rows[0] if rows else {}
        return payload.get("name", "")

    def _sync_get_interfaces(self, runner: SyncCommandRunner, _: dict[str, Any]) -> list[InterfaceInfo]:
        rows = runner.execute("/interface", "print")
        return [self._map_interface(row) for row in rows]

    def _sync_get_hotspot_servers(self, runner: SyncCommandRunner, _: dict[str, Any]) -> list[HotspotServerInfo]:
        rows = runner.execute("/ip/hotspot", "print")
        return [
            HotspotServerInfo(
                id=row.get(".id"),
                name=row.get("name", ""),
                interface=row.get("interface"),
                profile=row.get("profile"),
                disabled=_to_bool(row.get("disabled")),
                dns_name=row.get("dns-name"),
            )
            for row in rows
        ]

    def _sync_get_active_users(self, runner: SyncCommandRunner, _: dict[str, Any]) -> list[ActiveUserInfo]:
        rows = runner.execute("/ip/hotspot/active", "print")
        return [
            ActiveUserInfo(
                id=row.get(".id"),
                user=row.get("user"),
                address=row.get("address"),
                mac_address=row.get("mac-address"),
                uptime=row.get("uptime"),
                bytes_in=_to_int(row.get("bytes-in")),
                bytes_out=_to_int(row.get("bytes-out")),
                session_time_left=row.get("session-time-left"),
            )
            for row in rows
        ]

    def _sync_get_radius_config(self, runner: SyncCommandRunner, _: dict[str, Any]) -> list[RadiusConfig]:
        rows = runner.execute("/radius", "print")
        return [
            RadiusConfig(
                id=_routeros_id(row),
                service=row.get("service"),
                address=row.get("address"),
                secret=row.get("secret"),
                authentication_port=_to_int(row.get("authentication-port")),
                accounting_port=_to_int(row.get("accounting-port")),
                disabled=_to_bool(row.get("disabled")),
            )
            for row in rows
        ]

    def _sync_set_radius_server(self, runner: SyncCommandRunner, data: dict[str, Any]) -> str:
        rows = runner.execute("/radius", "print")
        matching = None
        for row in rows:
            service_value = row.get("service") or ""
            services = {part.strip() for part in str(service_value).split(",") if part.strip()}
            if data["service"] in services or row.get("address") == data["radius_host"]:
                matching = row
                break

        params = {
            "service": data["service"],
            "address": data["radius_host"],
            "secret": data["radius_secret"],
            "authentication-port": data["auth_port"],
            "accounting-port": data["accounting_port"],
        }
        matching_id = _routeros_id(matching) if matching else None
        if matching and matching_id:
            params[".id"] = matching_id
            runner.execute("/radius", "set", params=params)
            return "RADIUS server updated"
        runner.execute("/radius", "add", params=params)
        return "RADIUS server added"

    def _sync_enable_hotspot_radius(self, runner: SyncCommandRunner, data: dict[str, Any]) -> str:
        servers = runner.execute("/ip/hotspot", "print")
        server = next((row for row in servers if row.get("name") == data["hotspot_server_name"]), None) or (servers[0] if servers else None)
        if server is None:
            raise MikroTikOperationError("Hotspot server not found", commands=runner.commands, status="offline")

        profiles = runner.execute("/ip/hotspot/profile", "print")
        profile_name = server.get("profile") or "hsprof1"
        profile = next((row for row in profiles if row.get("name") == profile_name), None) or next(
            (row for row in profiles if row.get("name") == "hsprof1"),
            None,
        )
        profile_id = _routeros_id(profile) if profile else None
        if profile is None or not profile_id:
            raise MikroTikOperationError("Hotspot profile not found", commands=runner.commands, status="offline")
        runner.execute("/ip/hotspot/profile", "set", params={".id": profile_id, "use-radius": "yes"})
        return f"Hotspot profile {profile.get('name') or profile_name} now uses RADIUS"

    def _sync_set_hotspot_dns_name(self, runner: SyncCommandRunner, data: dict[str, Any]) -> str:
        servers = runner.execute("/ip/hotspot", "print")
        server = servers[0] if servers else None
        if server is None:
            raise MikroTikOperationError("Hotspot server not found", commands=runner.commands, status="offline")

        profile_name = server.get("profile") or "hsprof1"
        profiles = runner.execute("/ip/hotspot/profile", "print")
        profile = next((row for row in profiles if row.get("name") == profile_name), None) or next(
            (row for row in profiles if row.get("name") == "hsprof1"),
            None,
        )
        profile_id = _routeros_id(profile) if profile else None
        if profile is None or not profile_id:
            raise MikroTikOperationError("Hotspot profile not found", commands=runner.commands, status="offline")

        runner.execute("/ip/hotspot/profile", "set", params={".id": profile_id, "dns-name": data["dns_name"]})
        return "Hotspot DNS name updated"

    def _sync_configure_external_portal(self, runner: SyncCommandRunner, data: dict[str, Any]) -> str:
        portal_base_url = (data.get("portal_base_url") or "").rstrip("/")
        if not portal_base_url:
            raise MikroTikOperationError("Portal public base URL is not configured", commands=runner.commands, status="offline")

        portal_url = f"{portal_base_url}/portal/mikrotik/login-template"
        parsed = urlsplit(portal_base_url)
        if not parsed.scheme or not parsed.netloc:
            raise MikroTikOperationError("Portal public base URL must be an absolute URL", commands=runner.commands, status="offline")

        servers = runner.execute("/ip/hotspot", "print")
        server = servers[0] if servers else None
        if server is None:
            raise MikroTikOperationError("Hotspot server not found", commands=runner.commands, status="offline")

        profile_name = server.get("profile") or "hsprof1"
        profiles = runner.execute("/ip/hotspot/profile", "print")
        profile = next((row for row in profiles if row.get("name") == profile_name), None) or next(
            (row for row in profiles if row.get("name") == "hsprof1"),
            None,
        )
        profile_id = _routeros_id(profile) if profile else None
        if profile is None or not profile_id:
            raise MikroTikOperationError("Hotspot profile not found", commands=runner.commands, status="offline")

        runner.execute("/ip/hotspot/profile", "set", params={".id": profile_id, "login-by": "cookie,http-pap,mac-cookie"})
        self._ensure_walled_garden_host(runner, parsed)
        self._ensure_walled_garden_ip(runner, parsed)
        self._fetch_hotspot_file(runner, portal_url, "hotspot/login.html")
        self._fetch_hotspot_file(runner, portal_url, "hotspot/rlogin.html")
        return "External captive portal configured"

    def _sync_execute_template_commands(self, runner: SyncCommandRunner, data: dict[str, Any]) -> str:
        commands_to_run = data["commands"]
        for command in commands_to_run:
            action = command.get("command", "set")
            params = dict(command.get("params") or {})
            if action == "set":
                params = self._resolve_named_set_params(runner, command["path"], params)
            runner.execute(command["path"], action, params=params)
        return "Template applied"

    def _resolve_named_set_params(self, runner: SyncCommandRunner, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if params.get(".id") or "name" not in params:
            return params

        rows = runner.execute(path, "print")
        target = next((row for row in rows if row.get("name") == params.get("name")), None)
        target_id = _routeros_id(target) if target else None
        if not target_id:
            raise MikroTikOperationError(
                f"Template set command could not find named item '{params.get('name')}' at {path}",
                commands=runner.commands,
                status="offline",
            )

        resolved = dict(params)
        resolved[".id"] = target_id
        resolved.pop("name", None)
        return resolved

    def _ensure_walled_garden_host(self, runner: SyncCommandRunner, parsed_url) -> None:
        rows = runner.execute("/ip/hotspot/walled-garden", "print")
        host = parsed_url.hostname or ""
        port = str(parsed_url.port or (443 if parsed_url.scheme == "https" else 80))
        for row in rows:
            if (row.get("dst-host") or "") == host and (row.get("dst-port") or "") == port:
                return
        runner.execute(
            "/ip/hotspot/walled-garden",
            "add",
            params={
                "action": "allow",
                "dst-host": host,
                "dst-port": port,
                "comment": "project-portal",
            },
        )

    def _fetch_hotspot_file(self, runner: SyncCommandRunner, url: str, dst_path: str) -> None:
        resource = runner.api.get_resource("/tool")
        entry = {
            "path": "/tool",
            "command": "fetch",
            "params": _sanitize_mapping({"url": url, "dst-path": dst_path, "mode": "http", "keep-result": "yes"}),
            "queries": {},
            "status": "running",
            "started_at": _utcnow().isoformat(),
        }
        runner.commands.append(entry)
        try:
            response = resource.call("fetch", arguments={"url": url, "dst-path": dst_path, "mode": "http", "keep-result": "yes"})
            entry["status"] = "success"
            entry["completed_at"] = _utcnow().isoformat()
            entry["response"] = _sanitize_value(response)
        except Exception as exc:
            entry["status"] = "failed"
            entry["completed_at"] = _utcnow().isoformat()
            entry["error"] = _normalize_error(exc)
            raise MikroTikOperationError(entry["error"], commands=runner.commands, status=_classify_status(entry["error"])) from exc

    def _ensure_walled_garden_ip(self, runner: SyncCommandRunner, parsed_url) -> None:
        host = parsed_url.hostname or ""
        try:
            dst_address = str(ipaddress.ip_address(host))
        except ValueError:
            return

        port = str(parsed_url.port or (443 if parsed_url.scheme == "https" else 80))
        rows = runner.execute("/ip/hotspot/walled-garden/ip", "print")
        for row in rows:
            if (
                (row.get("dst-address") or "") == dst_address
                and (row.get("dst-port") or "") == port
                and (row.get("protocol") or "").lower() in {"tcp", "6", ""}
            ):
                return

        runner.execute(
            "/ip/hotspot/walled-garden/ip",
            "add",
            params={
                "action": "accept",
                "dst-address": dst_address,
                "dst-port": port,
                "protocol": "tcp",
                "comment": "project-portal-ip",
            },
        )

    def _sync_verify_radius_host(self, runner: SyncCommandRunner, data: dict[str, Any]) -> str:
        rows = runner.execute("/radius", "print")
        exists = any((row.get("address") == data["radius_host"]) for row in rows)
        if not exists:
            raise MikroTikOperationError("Provision verification failed: RADIUS host not found", commands=runner.commands, status="offline")
        runner.execute("/ip/hotspot/active", "print")
        return "Provisioning verified"

    def _sync_disconnect_hotspot_user(self, runner: SyncCommandRunner, data: dict[str, Any]) -> str:
        runner.execute("/ip/hotspot/active", "remove", params={".id": data["active_id"]})
        return "Active hotspot user disconnected"

    def _sync_reboot_router(self, runner: SyncCommandRunner, _: dict[str, Any]) -> str:
        runner.execute("/system", "reboot")
        return "Router reboot command sent"

    def _map_interface(self, row: dict[str, Any]) -> InterfaceInfo:
        return InterfaceInfo(
            id=_routeros_id(row),
            name=row.get("name", ""),
            type=row.get("type"),
            running=_to_bool(row.get("running")),
            disabled=_to_bool(row.get("disabled")),
            mac_address=row.get("mac-address"),
            tx_byte=_to_int(row.get("tx-byte")),
            rx_byte=_to_int(row.get("rx-byte")),
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_error(exc: Exception) -> str:
    message = str(exc).strip()
    lowered = message.lower()
    if "timed out" in lowered:
        return "Connection timed out"
    if "refused" in lowered:
        return "Connection refused"
    if "invalid user name or password" in lowered or "invalid username or password" in lowered:
        return "Authentication failed"
    if "getaddrinfo" in lowered or "name or service not known" in lowered:
        return "Unable to resolve host"
    return message or exc.__class__.__name__


def _classify_status(message: str) -> str:
    lowered = (message or "").lower()
    if "timed out" in lowered:
        return "timeout"
    if "auth" in lowered or "password" in lowered:
        return "auth_failed"
    return "offline"


def _sanitize_mapping(data: dict[str, Any]) -> dict[str, Any]:
    return {key: ("***" if "password" in key.lower() or "secret" in key.lower() else _sanitize_value(value)) for key, value in data.items()}


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _sanitize_mapping(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if hasattr(value, "model_dump"):
        return _sanitize_value(value.model_dump())
    return value


def _routeros_id(row: dict[str, Any] | None) -> str | None:
    if not row:
        return None
    return row.get(".id") or row.get("id")


def _encode_mapping_for_routeros(data: dict[str, Any]) -> dict[str, Any]:
    return {key: _encode_value_for_routeros(value) for key, value in data.items()}


def _encode_value_for_routeros(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return value


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"true", "yes"}


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_uptime_seconds(value: str | None) -> int | None:
    if not value:
        return None
    pattern = re.compile(r"(?:(?P<weeks>\d+)w)?(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?")
    match = pattern.fullmatch(value)
    if not match:
        legacy = re.fullmatch(r"(?:(?P<weeks>\d+)w)?(?:(?P<days>\d+)d)?(?P<hours>\d+):(?P<minutes>\d+):(?P<seconds>\d+)", value)
        if not legacy:
            return None
        match = legacy
    parts = {key: int(val or 0) for key, val in match.groupdict().items()}
    return (
        parts.get("weeks", 0) * 604800
        + parts.get("days", 0) * 86400
        + parts.get("hours", 0) * 3600
        + parts.get("minutes", 0) * 60
        + parts.get("seconds", 0)
    )
