"""Unit tests for resolve_radius_host — the tunnel-aware RADIUS server address
resolver. VPN-only platform: a router with a WireGuard tunnel must point its
RADIUS client at the server's tunnel IP, not RADIUS_PUBLIC_HOST."""
from types import SimpleNamespace

from src.modules.mikrotik.api_service import RouterAccess
from src.modules.mikrotik.radius_host import resolve_radius_host


def _settings(tunnel_ip="10.100.0.1", public_host="34.122.11.114"):
    return SimpleNamespace(wg_server_tunnel_ip=tunnel_ip, radius_public_host=public_host)


def test_tunneled_router_resolves_to_server_tunnel_ip():
    # The router's own tunnel IP (.7) is only the *signal* that it is tunneled;
    # the address pushed to it must be the SERVER's tunnel IP (.1).
    router = SimpleNamespace(wg_tunnel_ip="10.100.0.7")
    assert resolve_radius_host(router, _settings()) == "10.100.0.1"


def test_non_tunneled_router_falls_back_to_public_host():
    router = SimpleNamespace(wg_tunnel_ip=None)
    assert resolve_radius_host(router, _settings()) == "34.122.11.114"


def test_router_without_tunnel_attribute_falls_back():
    assert resolve_radius_host(object(), _settings()) == "34.122.11.114"


def test_none_router_falls_back_to_public_host():
    assert resolve_radius_host(None, _settings()) == "34.122.11.114"


def test_router_access_carries_tunnel_ip_for_diagnostics():
    # diagnostics resolves the expected host from RouterAccess, which now carries
    # wg_tunnel_ip — a tunneled access must resolve to the server tunnel IP.
    access = RouterAccess(
        router_id="r1", host="10.100.0.7", api_port=8728, api_username="admin",
        api_password="x", use_ssl=False, nas_secret="s", wg_tunnel_ip="10.100.0.7",
    )
    assert resolve_radius_host(access, _settings()) == "10.100.0.1"


def test_router_access_without_tunnel_falls_back():
    access = RouterAccess(
        router_id="r1", host="192.168.1.1", api_port=8728, api_username="admin",
        api_password="x", use_ssl=False, nas_secret="s",
    )
    assert resolve_radius_host(access, _settings()) == "34.122.11.114"


def test_values_are_stripped():
    router = SimpleNamespace(wg_tunnel_ip="10.100.0.7")
    assert resolve_radius_host(router, _settings(tunnel_ip="  10.100.0.1  ")) == "10.100.0.1"
    assert resolve_radius_host(SimpleNamespace(wg_tunnel_ip=None), _settings(public_host="  1.2.3.4 ")) == "1.2.3.4"
