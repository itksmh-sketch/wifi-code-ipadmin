"""Router setup wizard — UI-driven full router configuration.

Each of the four sections (network / hotspot / radius / nat) exposes a read-only
``detect`` (reads live config from the router) and an idempotent ``apply`` (sends
RouterOS API commands). All router I/O reuses the shared :class:`MikroTikAPIService`
connection plumbing, which already prefers the WireGuard tunnel IP when the tunnel
is up and falls back to the router's direct IP.

Subnet maths and all validation are done server-side (constraint #8 — never trust
client pool ranges). The RADIUS shared secret is passed in by the route from the
decrypted DB value and never returned to the caller (constraint #1).
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

from src.modules.mikrotik.api_service import (
    MikroTikAPIService,
    MikroTikOperationError,
    SyncCommandRunner,
)

# RouterOS object names the wizard manages. Keeping them constant lets detect
# recognise what the wizard previously created and keeps apply idempotent.
BRIDGE_DEFAULT = "bridge-hotspot"
POOL_NAME = "hs-pool"
DHCP_SERVER_NAME = "dhcp-hotspot"
HOTSPOT_SERVER_NAME = "hotspot1"
NAT_COMMENT = "hotspot-nat"
WG_INTERFACE_PREFIXES = ("wg", "wireguard")

ALLOWED_PREFIXES = (22, 23, 24, 25, 26, 27, 28)
LEASE_TIMES = {"1h": "01:00:00", "4h": "04:00:00", "8h": "08:00:00", "24h": "1d"}


class SetupValidationError(Exception):
    """Raised when client-submitted setup values fail server-side validation."""


# ─── Subnet helpers (all server-side) ───────────────────────────────────────


@dataclass
class SubnetPlan:
    gateway_ip: str
    prefix: int
    network_address: str
    broadcast: str
    pool_start: str
    pool_end: str
    hotspot_network: str  # network/prefix, e.g. 192.168.10.0/24
    usable_hosts: int


def hosts_in_subnet(prefix: int) -> int:
    """Usable host count for a prefix: 2^(32-prefix) - 2."""
    return (1 << (32 - prefix)) - 2


def plan_subnet(gateway_ip: str, prefix: int) -> SubnetPlan:
    """Validate a gateway/prefix and derive the network, broadcast and default
    DHCP pool range. Raises :class:`SetupValidationError` on bad input."""
    if prefix not in ALLOWED_PREFIXES:
        raise SetupValidationError(f"Unsupported subnet prefix /{prefix}")
    try:
        gateway = ipaddress.IPv4Address(gateway_ip)
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise SetupValidationError(f"Invalid gateway IP: {gateway_ip}") from exc
    network = ipaddress.IPv4Network(f"{gateway_ip}/{prefix}", strict=False)
    if gateway == network.network_address or gateway == network.broadcast_address:
        raise SetupValidationError("Gateway IP cannot be the network or broadcast address")
    # Default pool runs from gateway+1 to broadcast-1 so the gateway is never
    # handed out to a client (spec: "pool start = gateway IP + 1").
    pool_start = min(gateway + 1, network.broadcast_address - 1)
    pool_end = network.broadcast_address - 1
    return SubnetPlan(
        gateway_ip=str(gateway),
        prefix=prefix,
        network_address=str(network.network_address),
        broadcast=str(network.broadcast_address),
        pool_start=str(pool_start),
        pool_end=str(pool_end),
        hotspot_network=f"{network.network_address}/{prefix}",
        usable_hosts=hosts_in_subnet(prefix),
    )


def validate_pool_range(plan: SubnetPlan, pool_start: str, pool_end: str) -> tuple[str, str]:
    """Ensure an operator-customised pool range stays inside the subnet and the
    gateway is not handed out. Returns the validated (start, end)."""
    network = ipaddress.IPv4Network(f"{plan.network_address}/{plan.prefix}")
    try:
        start = ipaddress.IPv4Address(pool_start)
        end = ipaddress.IPv4Address(pool_end)
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise SetupValidationError("Pool start/end must be valid IPv4 addresses") from exc
    if start not in network or end not in network:
        raise SetupValidationError("Pool range must be within the configured subnet")
    if start > end:
        raise SetupValidationError("Pool start must be less than or equal to pool end")
    gateway = ipaddress.IPv4Address(plan.gateway_ip)
    if start <= gateway <= end:
        raise SetupValidationError("Pool range must not include the gateway IP")
    return str(start), str(end)


def _is_wan_safe(name: str) -> bool:
    """A WireGuard tunnel interface must never be selected as the WAN (constraint #6)."""
    lowered = (name or "").lower()
    return not any(lowered.startswith(prefix) for prefix in WG_INTERFACE_PREFIXES)


# ─── Detect/apply sync operations (run inside the RouterOS connection) ───────
#
# Each function has signature (runner, data) -> result and is handed to
# MikroTikAPIService.run_operation, which opens the connection, records every
# command, and returns (result, commands).


def _find(rows: list[dict[str, Any]], key: str, value: str) -> dict[str, Any] | None:
    return next((row for row in rows if (row.get(key) or "") == value), None)


def _truthy(value: Any) -> bool:
    return str(value).lower() in {"true", "yes"}


# Network ---------------------------------------------------------------------


def _op_detect_network(runner: SyncCommandRunner, _: dict[str, Any]) -> dict[str, Any]:
    bridges = runner.execute("/interface/bridge", "print")
    ports = runner.execute("/interface/bridge/port", "print")
    addresses = runner.execute("/ip/address", "print")
    pools = runner.execute("/ip/pool", "print")
    dhcp_servers = runner.execute("/ip/dhcp-server", "print")
    dhcp_networks = runner.execute("/ip/dhcp-server/network", "print")
    return {
        "bridges": [{"name": b.get("name"), "comment": b.get("comment")} for b in bridges],
        "bridge_ports": [{"bridge": p.get("bridge"), "interface": p.get("interface")} for p in ports],
        "addresses": [
            {"address": a.get("address"), "interface": a.get("interface"), "network": a.get("network")}
            for a in addresses
        ],
        "pools": [{"name": p.get("name"), "ranges": p.get("ranges")} for p in pools],
        "dhcp_servers": [
            {"name": d.get("name"), "interface": d.get("interface"), "address_pool": d.get("address-pool"), "disabled": _truthy(d.get("disabled"))}
            for d in dhcp_servers
        ],
        "dhcp_networks": [
            {"address": n.get("address"), "gateway": n.get("gateway"), "dns_server": n.get("dns-server")}
            for n in dhcp_networks
        ],
    }


def _op_apply_network(runner: SyncCommandRunner, data: dict[str, Any]) -> str:
    bridge = data["bridge_name"]
    # 1. Bridge (idempotent)
    bridges = runner.execute("/interface/bridge", "print")
    if not _find(bridges, "name", bridge):
        runner.execute("/interface/bridge", "add", params={"name": bridge, "comment": "hotspot-bridge"})

    # 2. Bridge ports — skip interfaces already a member of any bridge (constraint #7)
    ports = runner.execute("/interface/bridge/port", "print")
    existing_members = {p.get("interface") for p in ports}
    for iface in data["interfaces"]:
        if iface in existing_members:
            continue
        runner.execute("/interface/bridge/port", "add", params={"bridge": bridge, "interface": iface})

    # 3. Gateway IP on the bridge (idempotent by address)
    address_cidr = f"{data['gateway_ip']}/{data['prefix']}"
    addresses = runner.execute("/ip/address", "print")
    if not any((a.get("address") or "").split()[0] == address_cidr for a in addresses):
        runner.execute("/ip/address", "add", params={"address": address_cidr, "interface": bridge})

    # 4. Address pool
    pool_name = data.get("pool_name") or POOL_NAME
    ranges = f"{data['pool_start']}-{data['pool_end']}"
    pools = runner.execute("/ip/pool", "print")
    existing_pool = _find(pools, "name", pool_name)
    if existing_pool and existing_pool.get(".id"):
        runner.execute("/ip/pool", "set", params={".id": existing_pool[".id"], "ranges": ranges})
    else:
        runner.execute("/ip/pool", "add", params={"name": pool_name, "ranges": ranges})

    # 5. DHCP server
    dhcp_servers = runner.execute("/ip/dhcp-server", "print")
    server_params = {
        "name": DHCP_SERVER_NAME,
        "interface": bridge,
        "address-pool": pool_name,
        "lease-time": LEASE_TIMES.get(data.get("lease_time") or "1h", "01:00:00"),
        "disabled": "no",
    }
    existing_server = _find(dhcp_servers, "name", DHCP_SERVER_NAME)
    if existing_server and existing_server.get(".id"):
        # 'name' is the match key, not a settable field on update — drop it.
        update_params = {k: v for k, v in server_params.items() if k != "name"}
        runner.execute("/ip/dhcp-server", "set", params={**update_params, ".id": existing_server[".id"]})
    else:
        runner.execute("/ip/dhcp-server", "add", params=server_params)

    # 6. DHCP network
    network_cidr = f"{data['network_address']}/{data['prefix']}"
    dhcp_networks = runner.execute("/ip/dhcp-server/network", "print")
    net_params = {"address": network_cidr, "gateway": data["gateway_ip"], "dns-server": data.get("dns") or "8.8.8.8"}
    existing_net = _find(dhcp_networks, "address", network_cidr)
    if existing_net and existing_net.get(".id"):
        runner.execute("/ip/dhcp-server/network", "set", params={**net_params, ".id": existing_net[".id"]})
    else:
        runner.execute("/ip/dhcp-server/network", "add", params=net_params)

    return "Network configured"


# Hotspot ---------------------------------------------------------------------


def _op_detect_hotspot(runner: SyncCommandRunner, _: dict[str, Any]) -> dict[str, Any]:
    servers = runner.execute("/ip/hotspot", "print")
    profiles = runner.execute("/ip/hotspot/profile", "print")
    bridges = runner.execute("/interface/bridge", "print")
    return {
        "servers": [
            {"name": s.get("name"), "interface": s.get("interface"), "profile": s.get("profile"), "disabled": _truthy(s.get("disabled")), "address_pool": s.get("address-pool")}
            for s in servers
        ],
        "profiles": [
            {"name": p.get("name"), "dns_name": p.get("dns-name"), "login_by": p.get("login-by"), "use_radius": _truthy(p.get("use-radius"))}
            for p in profiles
        ],
        "bridges": [b.get("name") for b in bridges],
    }


def _op_apply_hotspot(runner: SyncCommandRunner, data: dict[str, Any]) -> str:
    bridge = data["bridge_name"]
    pool_name = data.get("pool_name") or POOL_NAME

    # 1. Hotspot server (idempotent by name)
    servers = runner.execute("/ip/hotspot", "print")
    server = _find(servers, "name", HOTSPOT_SERVER_NAME)
    if server and server.get(".id"):
        runner.execute("/ip/hotspot", "set", params={".id": server[".id"], "interface": bridge, "address-pool": pool_name, "disabled": "no"})
        profile_name = server.get("profile") or "hsprof1"
    else:
        runner.execute("/ip/hotspot", "add", params={"name": HOTSPOT_SERVER_NAME, "interface": bridge, "address-pool": pool_name, "disabled": "no"})
        servers = runner.execute("/ip/hotspot", "print")
        server = _find(servers, "name", HOTSPOT_SERVER_NAME) or {}
        profile_name = server.get("profile") or "hsprof1"

    # 2. Server profile — login methods, RADIUS, DNS name
    profiles = runner.execute("/ip/hotspot/profile", "print")
    profile = _find(profiles, "name", profile_name) or _find(profiles, "name", "hsprof1")
    login_by = ",".join(data.get("login_by") or ["http-pap", "cookie"])
    profile_params = {
        "login-by": login_by,
        "use-radius": "yes",
        "nas-port-type": "wireless-802.11",
        "dns-name": data["dns_name"],
    }
    if profile and profile.get(".id"):
        runner.execute("/ip/hotspot/profile", "set", params={**profile_params, ".id": profile[".id"]})
    else:
        # No matching profile exists yet (fresh router). /ip/hotspot/profile/set is
        # item-based and needs a .id, so create the profile instead of trying to
        # set one by name, then point the hotspot server at it — otherwise the
        # server keeps using the built-in "default" profile and these RADIUS/login
        # settings never take effect.
        runner.execute("/ip/hotspot/profile", "add", params={**profile_params, "name": profile_name})
        if server.get(".id"):
            runner.execute("/ip/hotspot", "set", params={".id": server[".id"], "profile": profile_name})

    # 3. Default user profile — session/idle timeouts and devices-per-credential.
    #    (RouterOS keeps these on the user profile, not the server profile.)
    user_profiles = runner.execute("/ip/hotspot/user/profile", "print")
    default_profile = _find(user_profiles, "name", "default") or (user_profiles[0] if user_profiles else None)
    if default_profile and default_profile.get(".id"):
        up_params = {".id": default_profile[".id"]}
        # 0 = inherit from RADIUS for session timeout, 0 = disabled for idle
        up_params["session-timeout"] = _minutes_to_clock(data.get("session_timeout", 0))
        up_params["idle-timeout"] = "none" if not data.get("idle_timeout") else _minutes_to_clock(data["idle_timeout"])
        up_params["shared-users"] = data.get("addresses_per_mac", 2)
        runner.execute("/ip/hotspot/user/profile", "set", params=up_params)

    return "Hotspot configured"


def _minutes_to_clock(minutes: int | None) -> str:
    minutes = int(minutes or 0)
    if minutes <= 0:
        return "0s"
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


# RADIUS ----------------------------------------------------------------------


def _op_detect_radius(runner: SyncCommandRunner, _: dict[str, Any]) -> dict[str, Any]:
    rows = runner.execute("/radius", "print")
    # NOTE: secret intentionally omitted — never surface it to the caller.
    return {
        "entries": [
            {
                "id": row.get(".id"),
                "service": row.get("service"),
                "address": row.get("address"),
                "authentication_port": row.get("authentication-port"),
                "accounting_port": row.get("accounting-port"),
                "timeout": row.get("timeout"),
                "disabled": _truthy(row.get("disabled")),
            }
            for row in rows
        ]
    }


def _op_apply_radius(runner: SyncCommandRunner, data: dict[str, Any]) -> str:
    service = data.get("service") or "hotspot"
    radius_host = data["radius_host"]
    params = {
        "service": service,
        "address": radius_host,
        "secret": data["radius_secret"],  # server-side only; sanitised in the command log
        "authentication-port": data.get("auth_port", 1812),
        "accounting-port": data.get("accounting_port", 1813),
        "timeout": _ms_to_routeros_timeout(data.get("timeout", 3000)),
    }
    # 1. Upsert the RADIUS entry (match by service+address)
    rows = runner.execute("/radius", "print")
    matching = None
    for row in rows:
        services = {part.strip() for part in str(row.get("service") or "").split(",") if part.strip()}
        if row.get("address") == radius_host and (service in services or not services):
            matching = row
            break
    if matching and matching.get(".id"):
        runner.execute("/radius", "set", params={**params, ".id": matching[".id"]})
    else:
        runner.execute("/radius", "add", params=params)

    # 2. Enable use-radius on the hotspot profile(s)
    profiles = runner.execute("/ip/hotspot/profile", "print")
    for profile in profiles:
        if profile.get(".id") and profile.get("name") != "default":
            runner.execute("/ip/hotspot/profile", "set", params={".id": profile[".id"], "use-radius": "yes"})

    # 3. Verify the entry now exists
    verify = runner.execute("/radius", "print")
    if not any(row.get("address") == radius_host for row in verify):
        raise MikroTikOperationError("RADIUS verification failed: entry not found after apply", commands=runner.commands, status="offline")
    return "RADIUS configured"


def _ms_to_routeros_timeout(ms: int) -> str:
    ms = int(ms or 3000)
    seconds = max(ms // 1000, 0)
    millis = ms % 1000
    if millis:
        return f"{seconds}s{millis}ms" if seconds else f"{millis}ms"
    return f"{seconds}s"


# NAT & firewall --------------------------------------------------------------


def _op_detect_nat(runner: SyncCommandRunner, _: dict[str, Any]) -> dict[str, Any]:
    nat_rules = runner.execute("/ip/firewall/nat", "print")
    filter_rules = runner.execute("/ip/firewall/filter", "print")
    interfaces = runner.execute("/interface", "print")
    routes = runner.execute("/ip/route", "print")

    # Auto-detect WAN: the interface carrying the default route, never a wg tunnel.
    suggested_wan = None
    for route in routes:
        dst = route.get("dst-address") or ""
        if dst in ("0.0.0.0/0", "0.0.0.0"):
            candidate = route.get("interface") or route.get("immediate-gw") or ""
            candidate = candidate.split("%")[-1] if "%" in candidate else candidate
            if candidate and _is_wan_safe(candidate):
                suggested_wan = candidate
                break

    masq = [
        r for r in nat_rules
        if (r.get("action") == "masquerade") and (r.get("chain") == "srcnat")
    ]
    return {
        "nat_rules": [
            {"id": r.get(".id"), "chain": r.get("chain"), "action": r.get("action"), "src_address": r.get("src-address"), "out_interface": r.get("out-interface"), "comment": r.get("comment")}
            for r in nat_rules
        ],
        "filter_rules": [
            {"id": r.get(".id"), "chain": r.get("chain"), "action": r.get("action"), "connection_state": r.get("connection-state"), "comment": r.get("comment")}
            for r in filter_rules
        ],
        "interfaces": [
            {"name": i.get("name"), "type": i.get("type")}
            for i in interfaces
            if _is_wan_safe(i.get("name") or "")
        ],
        "suggested_wan": suggested_wan,
        "masquerade_count": len(masq),
    }


def _op_apply_nat(runner: SyncCommandRunner, data: dict[str, Any]) -> str:
    wan = data["wan_interface"]
    if not _is_wan_safe(wan):
        raise MikroTikOperationError("WAN interface cannot be a WireGuard tunnel interface", commands=runner.commands, status="offline")

    # 1. Masquerade rule (idempotent by comment)
    if data.get("enable_nat", True):
        nat_rules = runner.execute("/ip/firewall/nat", "print")
        if not _find(nat_rules, "comment", NAT_COMMENT):
            runner.execute(
                "/ip/firewall/nat",
                "add",
                params={
                    "chain": "srcnat",
                    "src-address": data["hotspot_network"],
                    "out-interface": wan,
                    "action": "masquerade",
                    "comment": NAT_COMMENT,
                },
            )

    # 2. Optional firewall filters (idempotent by comment)
    options = set(data.get("firewall_options") or [])
    if options:
        filters = runner.execute("/ip/firewall/filter", "print")
        existing_comments = {f.get("comment") for f in filters}
        if "established" in options and "allow-established" not in existing_comments:
            runner.execute("/ip/firewall/filter", "add", params={"chain": "forward", "connection-state": "established,related", "action": "accept", "comment": "allow-established"})
        if "invalid" in options and "drop-invalid" not in existing_comments:
            runner.execute("/ip/firewall/filter", "add", params={"chain": "forward", "connection-state": "invalid", "action": "drop", "comment": "drop-invalid"})
        if "icmp" in options and "allow-icmp" not in existing_comments:
            runner.execute("/ip/firewall/filter", "add", params={"chain": "input", "protocol": "icmp", "action": "accept", "comment": "allow-icmp"})

    return "NAT & firewall configured"


def _op_remove_duplicate_nat(runner: SyncCommandRunner, _: dict[str, Any]) -> str:
    """Remove all but the first wizard-created masquerade rule."""
    nat_rules = runner.execute("/ip/firewall/nat", "print")
    wizard_rules = [r for r in nat_rules if (r.get("comment") == NAT_COMMENT) and r.get(".id")]
    removed = 0
    for rule in wizard_rules[1:]:
        runner.execute("/ip/firewall/nat", "remove", params={".id": rule[".id"]})
        removed += 1
    return f"Removed {removed} duplicate NAT rule(s)"


# ─── Status derivation from detected config ─────────────────────────────────


def derive_network_status(detected: dict[str, Any], bridge_name: str = BRIDGE_DEFAULT) -> str:
    has_bridge = any(b.get("name") == bridge_name for b in detected.get("bridges", []))
    has_address = any(b.get("interface") == bridge_name for b in detected.get("addresses", []))
    has_dhcp = any(d.get("interface") == bridge_name for d in detected.get("dhcp_servers", []))
    flags = [has_bridge, has_address, has_dhcp]
    if all(flags):
        return "configured"
    if any(flags):
        return "partial"
    return "unconfigured"


def derive_hotspot_status(detected: dict[str, Any]) -> str:
    servers = detected.get("servers", [])
    if any(not s.get("disabled") for s in servers):
        return "configured"
    if servers:
        return "partial"
    return "unconfigured"


def derive_radius_status(detected: dict[str, Any], radius_host: str | None) -> str:
    entries = detected.get("entries", [])
    if radius_host and any(e.get("address") == radius_host for e in entries):
        return "configured"
    if entries:
        return "partial"
    return "unconfigured"


def derive_nat_status(detected: dict[str, Any]) -> str:
    return "configured" if detected.get("masquerade_count", 0) > 0 else "unconfigured"


# ─── Terminal command generation (mirrors what apply sends) ─────────────────


def network_terminal_commands(cfg: dict[str, Any]) -> list[str]:
    bridge = cfg["bridge_name"]
    pool_name = cfg.get("pool_name") or POOL_NAME
    lines = [
        f'/interface/bridge/add name={bridge} comment="hotspot-bridge"',
    ]
    for iface in cfg.get("interfaces", []):
        lines.append(f"/interface/bridge/port/add bridge={bridge} interface={iface}")
    lines.append(f"/ip/address/add address={cfg['gateway_ip']}/{cfg['prefix']} interface={bridge}")
    lines.append(f"/ip/pool/add name={pool_name} ranges={cfg['pool_start']}-{cfg['pool_end']}")
    lease = LEASE_TIMES.get(cfg.get("lease_time") or "1h", "01:00:00")
    lines.append(f"/ip/dhcp-server/add name={DHCP_SERVER_NAME} interface={bridge} address-pool={pool_name} lease-time={lease} disabled=no")
    lines.append(f"/ip/dhcp-server/network/add address={cfg['network_address']}/{cfg['prefix']} gateway={cfg['gateway_ip']} dns-server={cfg.get('dns') or '8.8.8.8'}")
    return lines


def hotspot_terminal_commands(cfg: dict[str, Any]) -> list[str]:
    bridge = cfg["bridge_name"]
    pool_name = cfg.get("pool_name") or POOL_NAME
    login_by = ",".join(cfg.get("login_by") or ["http-pap", "cookie"])
    return [
        f"/ip/hotspot/add name={HOTSPOT_SERVER_NAME} interface={bridge} address-pool={pool_name} disabled=no",
        f'/ip/hotspot/profile/set [find name=hsprof1] login-by={login_by} use-radius=yes nas-port-type=wireless-802.11 dns-name={cfg["dns_name"]}',
        f'/ip/hotspot/user/profile/set [find name=default] session-timeout={_minutes_to_clock(cfg.get("session_timeout", 0))} idle-timeout={"none" if not cfg.get("idle_timeout") else _minutes_to_clock(cfg["idle_timeout"])} shared-users={cfg.get("addresses_per_mac", 2)}',
    ]


def radius_terminal_commands(cfg: dict[str, Any]) -> list[str]:
    # The secret is shown as a placeholder — operators copy the real one from the
    # masked field. Never embed the plaintext secret in generated commands.
    return [
        f'/radius/add service={cfg.get("service") or "hotspot"} address={cfg["radius_host"]} secret=<your-router-secret> '
        f'authentication-port={cfg.get("auth_port", 1812)} accounting-port={cfg.get("accounting_port", 1813)} '
        f'timeout={_ms_to_routeros_timeout(cfg.get("timeout", 3000))}',
        "/ip/hotspot/profile/set [find name=hsprof1] use-radius=yes",
    ]


def nat_terminal_commands(cfg: dict[str, Any]) -> list[str]:
    lines = []
    if cfg.get("enable_nat", True):
        lines.append(
            f'/ip/firewall/nat/add chain=srcnat src-address={cfg["hotspot_network"]} '
            f'out-interface={cfg["wan_interface"]} action=masquerade comment="{NAT_COMMENT}"'
        )
    options = set(cfg.get("firewall_options") or [])
    if "established" in options:
        lines.append('/ip/firewall/filter/add chain=forward connection-state=established,related action=accept comment="allow-established"')
    if "invalid" in options:
        lines.append('/ip/firewall/filter/add chain=forward connection-state=invalid action=drop comment="drop-invalid"')
    if "icmp" in options:
        lines.append('/ip/firewall/filter/add chain=input protocol=icmp action=accept comment="allow-icmp"')
    return lines


# ─── Service facade ─────────────────────────────────────────────────────────


class RouterSetupService:
    """Thin facade over MikroTikAPIService for the four setup sections."""

    def __init__(self, api: MikroTikAPIService | None = None) -> None:
        self.api = api or MikroTikAPIService()

    async def detect(self, router_id: str, operation) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return await self.api.run_operation(router_id, operation)

    async def apply(self, router_id: str, operation, data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        return await self.api.run_operation(router_id, operation, data=data)

    # Convenience wrappers used by the routes -------------------------------
    async def detect_network(self, router_id: str):
        return await self.detect(router_id, _op_detect_network)

    async def apply_network(self, router_id: str, data: dict[str, Any]):
        return await self.apply(router_id, _op_apply_network, data)

    async def detect_hotspot(self, router_id: str):
        return await self.detect(router_id, _op_detect_hotspot)

    async def apply_hotspot(self, router_id: str, data: dict[str, Any]):
        return await self.apply(router_id, _op_apply_hotspot, data)

    async def detect_radius(self, router_id: str):
        return await self.detect(router_id, _op_detect_radius)

    async def apply_radius(self, router_id: str, data: dict[str, Any]):
        return await self.apply(router_id, _op_apply_radius, data)

    async def detect_nat(self, router_id: str):
        return await self.detect(router_id, _op_detect_nat)

    async def apply_nat(self, router_id: str, data: dict[str, Any]):
        return await self.apply(router_id, _op_apply_nat, data)

    async def remove_duplicate_nat(self, router_id: str):
        return await self.apply(router_id, _op_remove_duplicate_nat, {})
