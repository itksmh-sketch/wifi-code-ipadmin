from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ConnectionTestRequest(BaseModel):
    host: str
    port: int = Field(default=8728, ge=1, le=65535)
    username: str = "admin"
    password: str
    use_ssl: bool = False


class ConnectionTestResult(BaseModel):
    success: bool
    board_name: str | None = None
    ros_version: str | None = None
    error: str | None = None


class RouterCredentialsRequest(BaseModel):
    api_username: str
    api_password: str
    api_port: int = Field(default=8728, ge=1, le=65535)
    use_ssl: bool = False


class RouterCredentialsResponse(BaseModel):
    router_id: str
    api_username: str
    api_port: int
    use_ssl: bool
    connection_status: str
    last_connected_at: datetime | None = None


class ProvisionRequest(BaseModel):
    hotspot_interface: str | None = None
    dns_name: str
    template_id: str | None = None


class ProvisionStartResponse(BaseModel):
    log_id: str


class ProvisionStatusResponse(BaseModel):
    log_id: str
    router_id: str
    status: str
    progress: str | None = None
    error_message: str | None = None
    commands_executed: list[dict[str, Any]] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ApplyTemplateRequest(BaseModel):
    template_id: str
    dns_name: str | None = None


class ConfirmActionRequest(BaseModel):
    confirm: bool | None = None


class SystemInfo(BaseModel):
    board_name: str | None = None
    ros_version: str | None = None
    cpu_load: int | None = None
    free_memory: int | None = None
    total_memory: int | None = None
    uptime: str | None = None
    uptime_seconds: int | None = None
    architecture_name: str | None = None


class InterfaceInfo(BaseModel):
    id: str | None = None
    name: str
    type: str | None = None
    running: bool = False
    disabled: bool = False
    mac_address: str | None = None
    tx_byte: int | None = None
    rx_byte: int | None = None


class HotspotServerInfo(BaseModel):
    id: str | None = None
    name: str
    interface: str | None = None
    profile: str | None = None
    disabled: bool = False
    dns_name: str | None = None


class ActiveUserInfo(BaseModel):
    id: str | None = None
    user: str | None = None
    address: str | None = None
    mac_address: str | None = None
    uptime: str | None = None
    bytes_in: int | None = None
    bytes_out: int | None = None
    session_time_left: str | None = None


class RadiusConfig(BaseModel):
    id: str | None = None
    service: str | None = None
    address: str | None = None
    secret: str | None = None
    authentication_port: int | None = None
    accounting_port: int | None = None
    disabled: bool = False


class ProvisionResult(BaseModel):
    success: bool
    message: str | None = None
    commands_executed: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] | None = None


class DiagnosticCheck(BaseModel):
    name: str
    status: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class DiagnosticsResponse(BaseModel):
    router_id: str
    collected_at: datetime
    overall_status: str
    checks: list[DiagnosticCheck]


class RouterMetricResponse(BaseModel):
    id: str
    router_id: str
    collected_at: datetime
    cpu_load_percent: int | None = None
    memory_used_percent: int | None = None
    uptime_seconds: int | None = None
    active_sessions: int | None = None
    total_tx_bytes: int | None = None
    total_rx_bytes: int | None = None
    board_name: str | None = None
    ros_version: str | None = None


class ConfigTemplateRequest(BaseModel):
    name: str
    description: str | None = None
    template_data: dict[str, Any]
    is_default: bool = False


class ConfigTemplateUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    template_data: dict[str, Any] | None = None
    is_default: bool | None = None


class ConfigTemplateResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    template_data: dict[str, Any]
    is_default: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TempInterfacesRequest(BaseModel):
    host: str
    port: int = Field(default=8728, ge=1, le=65535)
    username: str = "admin"
    password: str
    use_ssl: bool = False


class DisconnectUserRequest(BaseModel):
    active_id: str
    voucher_id: str | None = None
    username: str | None = None
