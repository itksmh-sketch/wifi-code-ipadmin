from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ConfigTemplate, Router
from src.modules.mikrotik.api_service import MikroTikAPIService
from src.modules.mikrotik.types import ConfigTemplateRequest, ConfigTemplateUpdateRequest, ProvisionResult
from src.utils.encryption import decrypt_secret


class TemplateValidationError(ValueError):
    pass


class ConfigTemplateService:
    def validate_template_data(self, template_data: dict[str, Any]) -> None:
        if not isinstance(template_data, dict):
            raise TemplateValidationError("template_data must be an object")
        if template_data.get("version") != 1:
            raise TemplateValidationError("template_data.version must be 1")
        commands = template_data.get("commands")
        if not isinstance(commands, list) or not commands:
            raise TemplateValidationError("template_data.commands must be a non-empty array")
        for index, command in enumerate(commands):
            if not isinstance(command, dict):
                raise TemplateValidationError(f"command at index {index} must be an object")
            if not command.get("path"):
                raise TemplateValidationError(f"command at index {index} is missing path")
            if "params" in command and not isinstance(command["params"], dict):
                raise TemplateValidationError(f"command at index {index} params must be an object")

    async def list_templates(self, db: AsyncSession, isp_operator_id) -> list[ConfigTemplate]:
        result = await db.execute(
            select(ConfigTemplate)
            .where((ConfigTemplate.isp_operator_id == isp_operator_id) | (ConfigTemplate.isp_operator_id.is_(None)))
            .order_by(ConfigTemplate.name)
        )
        return list(result.scalars().all())

    async def create_template(self, db: AsyncSession, payload: ConfigTemplateRequest, isp_operator_id) -> ConfigTemplate:
        self.validate_template_data(payload.template_data)
        if payload.is_default:
            await db.execute(
                update(ConfigTemplate)
                .where(ConfigTemplate.isp_operator_id == isp_operator_id)
                .values(is_default=False)
            )
        template = ConfigTemplate(
            isp_operator_id=isp_operator_id,
            name=payload.name,
            description=payload.description,
            template_data=payload.template_data,
            is_default=payload.is_default,
        )
        db.add(template)
        await db.commit()
        await db.refresh(template)
        return template

    async def update_template(self, db: AsyncSession, template: ConfigTemplate, payload: ConfigTemplateUpdateRequest, isp_operator_id) -> ConfigTemplate:
        if template.isp_operator_id != isp_operator_id:
            raise TemplateValidationError("Platform templates cannot be modified by tenant admins")
        if payload.template_data is not None:
            self.validate_template_data(payload.template_data)
            template.template_data = payload.template_data
        if payload.name is not None:
            template.name = payload.name
        if payload.description is not None:
            template.description = payload.description
        if payload.is_default is not None:
            if payload.is_default:
                await db.execute(
                    update(ConfigTemplate)
                    .where(ConfigTemplate.id != template.id, ConfigTemplate.isp_operator_id == isp_operator_id)
                    .values(is_default=False)
                )
            template.is_default = payload.is_default
        await db.commit()
        await db.refresh(template)
        return template

    async def apply_template(
        self,
        db: AsyncSession,
        *,
        router_id: str,
        template_id: str,
        dns_name: str | None = None,
    ) -> ProvisionResult:
        template = await db.get(ConfigTemplate, template_id)
        if template is None:
            raise TemplateValidationError("Config template not found")
        router = await db.get(Router, router_id)
        if router is None:
            raise TemplateValidationError("Router not found")

        rendered = self.render_template(
            template.template_data,
            radius_host=self._radius_host(),
            nas_secret=decrypt_secret(router.nas_secret),
            dns_name=dns_name,
        )
        service = MikroTikAPIService()
        return await service.execute_template_commands(router_id, rendered)

    def render_template(
        self,
        template_data: dict[str, Any],
        *,
        radius_host: str,
        nas_secret: str,
        dns_name: str | None,
    ) -> list[dict[str, Any]]:
        self.validate_template_data(template_data)
        rendered: list[dict[str, Any]] = []
        for command in template_data["commands"]:
            path, action = self._split_path_and_command(command["path"])
            params = {
                key: self._substitute_value(value, radius_host=radius_host, nas_secret=nas_secret, dns_name=dns_name)
                for key, value in (command.get("params") or {}).items()
            }
            rendered.append(
                {
                    "path": path,
                    "command": action,
                    "params": params,
                }
            )
        return rendered

    def _substitute_value(self, value: Any, *, radius_host: str, nas_secret: str, dns_name: str | None) -> Any:
        if not isinstance(value, str):
            return value
        return (
            value.replace("{RADIUS_HOST}", radius_host)
            .replace("{NAS_SECRET}", nas_secret)
            .replace("{DNS_NAME}", dns_name or "")
        )

    def _split_path_and_command(self, path: str) -> tuple[str, str]:
        lowered = path.lower().rstrip("/")
        for action in ("add", "set", "remove"):
            suffix = f"/{action}"
            if lowered.endswith(suffix):
                return path[: -len(suffix)], action
        return path, "set"

    def _radius_host(self) -> str:
        from src.config import get_settings

        settings = get_settings()
        return settings.radius_public_host
