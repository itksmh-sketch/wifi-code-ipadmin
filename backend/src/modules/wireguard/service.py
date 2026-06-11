"""WireGuard tunnel service.

Responsibilities:
  * Allocate stable tunnel IPs from ``WG_TUNNEL_NETWORK`` (atomically).
  * Talk to the ``wg-manager`` sidecar over HTTP to add/remove peers and
    read peer status. The sidecar runs on the host network so it can touch
    the real ``wg0`` interface; the backend never runs ``wg`` itself.
  * Build the ready-to-paste MikroTik configuration for operators.

Key generation lives in :mod:`src.modules.wireguard.keys`.
"""
import ipaddress
import urllib.parse

import httpx
import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.models import Router, WgIpAllocation

logger = structlog.get_logger(__name__)
settings = get_settings()

# Stable advisory-lock key so concurrent allocations serialize on the IP pool.
_WG_ALLOC_LOCK_KEY = 0x7705A110C  # arbitrary, "wg alloc"


class WireGuardError(RuntimeError):
    """Raised when a WireGuard operation cannot be completed."""


class WireGuardService:
    def __init__(self, manager_url: str | None = None, timeout: float = 5.0):
        self._manager_url = (manager_url or settings.wg_manager_url).rstrip("/")
        self._timeout = timeout

    # ----- IP allocation -------------------------------------------------

    async def allocate_tunnel_ip(self, db: AsyncSession, router_id: str) -> str:
        """Allocate the next free IP in ``WG_TUNNEL_NETWORK`` for ``router_id``.

        Atomic: serialized via a transaction-scoped Postgres advisory lock so
        two routers can never receive the same IP. If the router already has an
        allocation it is returned unchanged (idempotent). Runs inside the
        caller's transaction — the caller commits.
        """
        # Serialize the read-then-insert within this transaction.
        await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _WG_ALLOC_LOCK_KEY})

        existing = await db.execute(
            select(WgIpAllocation.tunnel_ip).where(WgIpAllocation.router_id == router_id)
        )
        current = existing.scalar_one_or_none()
        if current is not None:
            return str(current)

        network = ipaddress.ip_network(settings.wg_tunnel_network)
        server_ip = ipaddress.ip_address(settings.wg_server_tunnel_ip)

        used_rows = await db.execute(select(WgIpAllocation.tunnel_ip))
        used = {str(row[0]) for row in used_rows.fetchall()}
        used.add(str(server_ip))  # never hand out the server's own IP

        for host in network.hosts():
            candidate = str(host)
            if candidate not in used:
                db.add(WgIpAllocation(router_id=router_id, tunnel_ip=candidate))
                await db.flush()
                logger.info("wg_ip_allocated", router_id=str(router_id), tunnel_ip=candidate)
                return candidate

        raise WireGuardError("No available tunnel IPs — maximum routers reached")

    async def deallocate_tunnel_ip(self, db: AsyncSession, router_id: str) -> None:
        """Release a router's tunnel IP back to the pool. Runs in caller's txn."""
        result = await db.execute(
            select(WgIpAllocation).where(WgIpAllocation.router_id == router_id)
        )
        allocation = result.scalar_one_or_none()
        if allocation is not None:
            await db.delete(allocation)
            await db.flush()

    # ----- wg-manager sidecar (peer management on the host) --------------

    async def add_peer(self, public_key: str, tunnel_ip: str) -> None:
        """Add a peer to the running wg0 interface via the sidecar."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._manager_url}/peer",
                    json={"public_key": public_key, "allowed_ip": f"{tunnel_ip}/32"},
                )
            if resp.status_code >= 400:
                raise WireGuardError(f"wg-manager add_peer failed: {resp.status_code} {resp.text}")
        except httpx.HTTPError as exc:
            raise WireGuardError(f"wg-manager unreachable: {exc}") from exc

    async def remove_peer(self, public_key: str) -> None:
        """Remove a peer from wg0 via the sidecar. Best-effort."""
        quoted = urllib.parse.quote(public_key, safe="")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.delete(f"{self._manager_url}/peer/{quoted}")
            if resp.status_code >= 400 and resp.status_code != 404:
                raise WireGuardError(f"wg-manager remove_peer failed: {resp.status_code} {resp.text}")
        except httpx.HTTPError as exc:
            raise WireGuardError(f"wg-manager unreachable: {exc}") from exc

    async def get_peer_status(self, public_key: str) -> dict:
        """Return ``{connected, last_handshake, transfer_rx, transfer_tx}`` for a peer.

        Raises :class:`WireGuardError` if the sidecar is unreachable so callers
        (e.g. the monitoring job) can decide to log-and-continue.
        """
        quoted = urllib.parse.quote(public_key, safe="")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self._manager_url}/peer/{quoted}/status")
            if resp.status_code == 404:
                return {"connected": False, "last_handshake": None, "transfer_rx": 0, "transfer_tx": 0}
            if resp.status_code >= 400:
                raise WireGuardError(f"wg-manager status failed: {resp.status_code} {resp.text}")
            return resp.json()
        except httpx.HTTPError as exc:
            raise WireGuardError(f"wg-manager unreachable: {exc}") from exc

    async def list_peers(self) -> list[dict]:
        """List all peers with status from the sidecar."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self._manager_url}/peers")
            if resp.status_code >= 400:
                raise WireGuardError(f"wg-manager list failed: {resp.status_code} {resp.text}")
            return resp.json()
        except httpx.HTTPError as exc:
            raise WireGuardError(f"wg-manager unreachable: {exc}") from exc

    # ----- MikroTik config builder ---------------------------------------

    def build_mikrotik_commands(self, router_private_key: str, tunnel_ip: str, endpoint: str | None = None) -> str:
        """Return ready-to-paste MikroTik RouterOS commands for the operator.

        ``endpoint`` is the platform-level WG_SERVER_ENDPOINT (read from the
        platform_settings table by the caller); falls back to the .env default.
        Never includes the server's private key — only its public key.
        """
        endpoint = endpoint or settings.wg_server_endpoint
        if ":" in endpoint:
            endpoint_addr, endpoint_port = endpoint.rsplit(":", 1)
        else:
            endpoint_addr, endpoint_port = endpoint, "51820"
        network = settings.wg_tunnel_network
        prefix = ipaddress.ip_network(network).prefixlen

        return (
            "# Step 1: Add WireGuard interface\n"
            f'/interface/wireguard/add name=wg-hotspot private-key="{router_private_key}"\n'
            "\n"
            "# Step 2: Add VPN server as peer\n"
            "/interface/wireguard/peers/add \\\n"
            "  interface=wg-hotspot \\\n"
            f'  public-key="{settings.wg_server_public_key}" \\\n'
            f"  endpoint-address={endpoint_addr} \\\n"
            f"  endpoint-port={endpoint_port} \\\n"
            f"  allowed-address={network} \\\n"
            "  persistent-keepalive=25\n"
            "\n"
            "# Step 3: Assign tunnel IP to the interface\n"
            f"/ip/address/add address={tunnel_ip}/{prefix} interface=wg-hotspot\n"
            "\n"
            "# Step 4: Done! Your router will connect automatically.\n"
            f"# Test with: /ping {settings.wg_server_tunnel_ip}\n"
        )
