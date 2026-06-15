from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from src.config import get_settings
from src.db.base import async_session_factory
from src.db.models import ConfigTemplate, Router, RouterCredential, RouterProvisionLog
from src.modules.mikrotik.api_service import MikroTikAPIService, MikroTikOperationError, RouterCredentialsMissingError
from src.modules.mikrotik.template_engine import ConfigTemplateService, TemplateValidationError
from src.utils.portal_token import create_portal_router_token

settings = get_settings()


class MikroTikProvisioner:
    def __init__(self) -> None:
        self.api_service = MikroTikAPIService()
        self.template_service = ConfigTemplateService()
        self._tasks: set[asyncio.Task] = set()

    def launch_provision(
        self,
        *,
        router_id: str,
        log_id: str,
        dns_name: str,
        hotspot_interface: str | None,
        template_id: str | None,
    ) -> None:
        task = asyncio.create_task(
            self._run_provision(
                router_id=router_id,
                log_id=log_id,
                dns_name=dns_name,
                hotspot_interface=hotspot_interface,
                template_id=template_id,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def launch_apply_template(self, *, router_id: str, log_id: str, template_id: str, dns_name: str | None = None) -> None:
        task = asyncio.create_task(self._run_apply_template(router_id=router_id, log_id=log_id, template_id=template_id, dns_name=dns_name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_provision(self, *, router_id: str, log_id: str, dns_name: str, hotspot_interface: str | None, template_id: str | None) -> None:
        try:
            await self._append_step_meta(log_id, {"step": "starting", "message": "Provisioning started", "hotspot_interface": hotspot_interface})
            result = await self.api_service.set_radius_server(
                router_id,
                radius_host=settings.radius_public_host,
                radius_secret=await self._get_router_nas_secret(router_id),
            )
            await self._append_commands(log_id, result.commands_executed)

            result = await self.api_service.enable_hotspot_radius(router_id)
            await self._append_commands(log_id, result.commands_executed)

            result = await self.api_service.set_hotspot_dns_name(router_id, dns_name=dns_name)
            await self._append_commands(log_id, result.commands_executed)

            result = await self.api_service.configure_external_portal(
                router_id,
                settings.effective_portal_public_base_url,
                create_portal_router_token(router_id),
            )
            await self._append_commands(log_id, result.commands_executed)

            if template_id:
                await self._append_step_meta(log_id, {"step": "apply_template", "message": "Applying config template"})
                async with async_session_factory() as db:
                    result = await self.template_service.apply_template(db, router_id=router_id, template_id=template_id, dns_name=dns_name)
                await self._append_commands(log_id, result.commands_executed)

            result = await self.api_service.verify_radius_host(router_id, settings.radius_public_host)
            await self._append_commands(log_id, result.commands_executed)
            await self._mark_log_complete(log_id, status="success")
        except (MikroTikOperationError, RouterCredentialsMissingError, TemplateValidationError) as exc:
            await self._append_commands(log_id, getattr(exc, "commands", []))
            await self._mark_log_complete(log_id, status="failed", error_message=str(exc))
        except Exception as exc:  # pragma: no cover - safety net
            await self._mark_log_complete(log_id, status="failed", error_message=str(exc))

    async def _run_apply_template(self, *, router_id: str, log_id: str, template_id: str, dns_name: str | None) -> None:
        try:
            async with async_session_factory() as db:
                result = await self.template_service.apply_template(db, router_id=router_id, template_id=template_id, dns_name=dns_name)
            await self._append_commands(log_id, result.commands_executed)
            await self._mark_log_complete(log_id, status="success")
        except (MikroTikOperationError, RouterCredentialsMissingError, TemplateValidationError) as exc:
            await self._append_commands(log_id, getattr(exc, "commands", []))
            await self._mark_log_complete(log_id, status="failed", error_message=str(exc))
        except Exception as exc:  # pragma: no cover
            await self._mark_log_complete(log_id, status="failed", error_message=str(exc))

    async def ensure_router_credentials(self, router_id: str) -> None:
        async with async_session_factory() as db:
            result = await db.execute(select(RouterCredential).where(RouterCredential.router_id == router_id))
            if result.scalar_one_or_none() is None:
                raise RouterCredentialsMissingError("Router credentials are required before provisioning")

    async def _get_router_nas_secret(self, router_id: str) -> str:
        from src.utils.encryption import decrypt_secret

        async with async_session_factory() as db:
            router = await db.get(Router, router_id)
            if router is None:
                raise ValueError("Router not found")
            return decrypt_secret(router.nas_secret)

    async def _append_step_meta(self, log_id: str, entry: dict[str, Any]) -> None:
        async with async_session_factory() as db:
            log_row = await db.get(RouterProvisionLog, log_id)
            if log_row is None:
                return
            commands = list(log_row.commands_executed or [])
            payload = dict(entry)
            payload.setdefault("status", "info")
            payload.setdefault("started_at", datetime.now(timezone.utc).isoformat())
            commands.append(payload)
            log_row.commands_executed = commands
            await db.commit()

    async def _append_commands(self, log_id: str, commands_to_add: list[dict[str, Any]]) -> None:
        if not commands_to_add:
            return
        async with async_session_factory() as db:
            log_row = await db.get(RouterProvisionLog, log_id)
            if log_row is None:
                return
            commands = list(log_row.commands_executed or [])
            commands.extend(commands_to_add)
            log_row.commands_executed = commands
            await db.commit()

    async def _mark_log_complete(self, log_id: str, *, status: str, error_message: str | None = None) -> None:
        async with async_session_factory() as db:
            log_row = await db.get(RouterProvisionLog, log_id)
            if log_row is None:
                return
            log_row.status = status
            log_row.error_message = error_message
            log_row.completed_at = datetime.now(timezone.utc)
            await db.commit()
