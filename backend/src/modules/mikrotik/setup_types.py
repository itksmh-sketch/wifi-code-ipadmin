"""Request/response schemas for the router setup wizard endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class NetworkApplyRequest(BaseModel):
    bridge_name: str = "bridge-hotspot"
    interfaces: list[str] = Field(default_factory=list)
    gateway_ip: str
    prefix: int = Field(default=24, ge=22, le=28)
    pool_start: str | None = None  # validated server-side; defaults derived from subnet
    pool_end: str | None = None
    dns: str = "8.8.8.8"
    lease_time: str = "1h"


class HotspotApplyRequest(BaseModel):
    bridge_name: str = "bridge-hotspot"
    dns_name: str
    login_by: list[str] = Field(default_factory=lambda: ["http-pap", "cookie"])
    session_timeout: int = Field(default=0, ge=0)
    idle_timeout: int = Field(default=0, ge=0)
    addresses_per_mac: int = Field(default=2, ge=1)


class RadiusApplyRequest(BaseModel):
    # Server address and shared secret come from the platform/DB, never the client.
    timeout: int = Field(default=3000, ge=100, le=60000)


class NatApplyRequest(BaseModel):
    wan_interface: str
    hotspot_network: str | None = None  # defaults to the network applied in section 1
    enable_nat: bool = True
    firewall_options: list[str] = Field(default_factory=list)
    remove_duplicates: bool = False


class SectionStatus(BaseModel):
    status: str = "unconfigured"
    detected_at: datetime | None = None
    last_applied_at: datetime | None = None
    config: dict[str, Any] | None = None


class SetupStatusResponse(BaseModel):
    router_id: str
    online: bool
    sections_complete: int
    total_sections: int = 4
    network: SectionStatus
    hotspot: SectionStatus
    radius: SectionStatus
    nat: SectionStatus


class ApplyResultResponse(BaseModel):
    success: bool
    message: str
    status: str
    last_applied_at: datetime | None = None
    commands_executed: list[dict[str, Any]] = Field(default_factory=list)
    terminal_commands: list[str] = Field(default_factory=list)


class NasSecretResponse(BaseModel):
    masked: str
    hint: str
