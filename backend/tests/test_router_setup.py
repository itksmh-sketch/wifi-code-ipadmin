"""Tests for the router setup wizard — subnet maths, the four sections' detect/
apply command sequences (via an in-process fake RouterOS runner), terminal
command generation, and the live HTTP endpoints (auth, offline guard, status,
masked NAS secret, tenant isolation)."""
import uuid

import pytest

from test_multi_tenant_security import _request
from test_multi_tenancy import _create_network, _create_operator_pair

from src.modules.mikrotik import setup_service as svc
from src.modules.mikrotik.api_service import MikroTikAPIService, MikroTikOperationError, _sanitize_mapping


# ─── Fake RouterOS runner ───────────────────────────────────────────────────


class FakeRunner:
    """Stands in for SyncCommandRunner. ``responses`` maps (path, command) to the
    rows a print returns; every call is recorded in ``calls`` for assertions."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
        self.commands = []
        self.api = None

    def execute(self, path, command="print", *, params=None, queries=None):
        self.calls.append({"path": path, "command": command, "params": dict(params or {})})
        resp = self.responses.get((path, command))
        return resp if resp is not None else []

    def adds(self, path):
        return [c for c in self.calls if c["path"] == path and c["command"] == "add"]

    def sets(self, path):
        return [c for c in self.calls if c["path"] == path and c["command"] == "set"]


# ─── Subnet maths (server-side validation) ──────────────────────────────────


def test_plan_subnet_derives_pool_and_hosts():
    plan = svc.plan_subnet("192.168.10.1", 24)
    assert plan.network_address == "192.168.10.0"
    assert plan.broadcast == "192.168.10.255"
    assert plan.pool_start == "192.168.10.2"
    assert plan.pool_end == "192.168.10.254"
    assert plan.hotspot_network == "192.168.10.0/24"
    assert plan.usable_hosts == 254


@pytest.mark.parametrize("prefix,hosts", [(24, 254), (25, 126), (26, 62), (27, 30), (28, 14), (23, 510), (22, 1022)])
def test_hosts_in_subnet(prefix, hosts):
    assert svc.hosts_in_subnet(prefix) == hosts


def test_plan_subnet_rejects_bad_input():
    with pytest.raises(svc.SetupValidationError):
        svc.plan_subnet("999.1.1.1", 24)
    with pytest.raises(svc.SetupValidationError):
        svc.plan_subnet("192.168.10.1", 30)  # unsupported prefix
    with pytest.raises(svc.SetupValidationError):
        svc.plan_subnet("192.168.10.0", 24)  # gateway == network address


def test_validate_pool_range_enforces_bounds():
    plan = svc.plan_subnet("192.168.10.1", 24)
    # Out of subnet
    with pytest.raises(svc.SetupValidationError):
        svc.validate_pool_range(plan, "192.168.11.2", "192.168.11.50")
    # Includes the gateway
    with pytest.raises(svc.SetupValidationError):
        svc.validate_pool_range(plan, "192.168.10.1", "192.168.10.50")
    # Start after end
    with pytest.raises(svc.SetupValidationError):
        svc.validate_pool_range(plan, "192.168.10.50", "192.168.10.10")
    # Valid
    assert svc.validate_pool_range(plan, "192.168.10.10", "192.168.10.100") == ("192.168.10.10", "192.168.10.100")


# ─── Section 1: Network apply command sequence ──────────────────────────────


def _network_data(interfaces=("ether2", "ether3")):
    return {
        "bridge_name": "bridge-hotspot",
        "interfaces": list(interfaces),
        "gateway_ip": "192.168.10.1",
        "prefix": 24,
        "pool_start": "192.168.10.2",
        "pool_end": "192.168.10.254",
        "network_address": "192.168.10.0",
        "dns": "8.8.8.8",
        "lease_time": "1h",
        "pool_name": "hs-pool",
    }


def test_network_apply_on_empty_router_creates_everything():
    runner = FakeRunner()  # all prints return []
    svc._op_apply_network(runner, _network_data())
    assert len(runner.adds("/interface/bridge")) == 1
    assert {c["params"]["interface"] for c in runner.adds("/interface/bridge/port")} == {"ether2", "ether3"}
    assert runner.adds("/ip/address")[0]["params"]["address"] == "192.168.10.1/24"
    assert runner.adds("/ip/pool")[0]["params"]["ranges"] == "192.168.10.2-192.168.10.254"
    assert runner.adds("/ip/dhcp-server")[0]["params"]["interface"] == "bridge-hotspot"
    assert runner.adds("/ip/dhcp-server/network")[0]["params"]["gateway"] == "192.168.10.1"


def test_network_apply_is_idempotent_and_skips_existing_bridge_member():
    # Bridge already exists, ether2 already a member of some bridge, pool/dhcp exist.
    runner = FakeRunner({
        ("/interface/bridge", "print"): [{".id": "*1", "name": "bridge-hotspot"}],
        ("/interface/bridge/port", "print"): [{"bridge": "bridge-hotspot", "interface": "ether2"}],
        ("/ip/address", "print"): [{"address": "192.168.10.1/24", "interface": "bridge-hotspot"}],
        ("/ip/pool", "print"): [{".id": "*5", "name": "hs-pool", "ranges": "x"}],
        ("/ip/dhcp-server", "print"): [{".id": "*7", "name": "dhcp-hotspot"}],
        ("/ip/dhcp-server/network", "print"): [{".id": "*9", "address": "192.168.10.0/24"}],
    })
    svc._op_apply_network(runner, _network_data())
    assert runner.adds("/interface/bridge") == []                 # not re-added
    # only ether3 added — ether2 is already a member (constraint #7)
    added_ports = [c["params"]["interface"] for c in runner.adds("/interface/bridge/port")]
    assert added_ports == ["ether3"]
    assert runner.adds("/ip/address") == []                       # address already present
    assert runner.adds("/ip/pool") == [] and runner.sets("/ip/pool")  # updated, not added
    assert runner.adds("/ip/dhcp-server") == [] and runner.sets("/ip/dhcp-server")


# ─── Section 2: Hotspot apply ───────────────────────────────────────────────


def test_hotspot_apply_creates_server_and_enables_radius():
    runner = FakeRunner({
        ("/ip/hotspot", "print"): [],
        ("/ip/hotspot/profile", "print"): [{".id": "*1", "name": "hsprof1"}],
        ("/ip/hotspot/user/profile", "print"): [{".id": "*2", "name": "default"}],
    })
    data = {"bridge_name": "bridge-hotspot", "dns_name": "hotspot.acme.local", "login_by": ["http-pap", "cookie"],
            "session_timeout": 0, "idle_timeout": 0, "addresses_per_mac": 2, "pool_name": "hs-pool"}
    svc._op_apply_hotspot(runner, data)
    assert runner.adds("/ip/hotspot")[0]["params"]["name"] == "hotspot1"
    prof_set = runner.sets("/ip/hotspot/profile")[0]["params"]
    assert prof_set["use-radius"] == "yes"
    assert prof_set["dns-name"] == "hotspot.acme.local"
    assert prof_set["login-by"] == "http-pap,cookie"
    up_set = runner.sets("/ip/hotspot/user/profile")[0]["params"]
    assert up_set["shared-users"] == 2


def test_hotspot_apply_creates_profile_when_missing_and_assigns_it():
    # Fresh router: a hotspot server exists but the named profile does not yet.
    # /ip/hotspot/profile/set is item-based, so we must add the profile (not set
    # it by name) and then point the server at it.
    runner = FakeRunner({
        ("/ip/hotspot", "print"): [{".id": "*7", "name": "hotspot1"}],
        ("/ip/hotspot/profile", "print"): [],
        ("/ip/hotspot/user/profile", "print"): [{".id": "*2", "name": "default"}],
    })
    data = {"bridge_name": "bridge-hotspot", "dns_name": "hotspot.acme.local", "login_by": ["http-pap", "cookie"],
            "session_timeout": 0, "idle_timeout": 0, "addresses_per_mac": 2, "pool_name": "hs-pool"}
    svc._op_apply_hotspot(runner, data)

    # No profile/set without a .id (the original bug produced exactly that).
    assert all(".id" in c["params"] for c in runner.sets("/ip/hotspot/profile"))

    prof_add = runner.adds("/ip/hotspot/profile")[0]["params"]
    assert prof_add["name"] == "hsprof1"
    assert prof_add["use-radius"] == "yes"
    assert prof_add["nas-port-type"] == "wireless-802.11"
    assert prof_add["dns-name"] == "hotspot.acme.local"
    assert prof_add["login-by"] == "http-pap,cookie"

    # The server is repointed at the freshly created profile.
    server_set = [c for c in runner.sets("/ip/hotspot") if c["params"].get("profile") == "hsprof1"]
    assert server_set and server_set[0]["params"][".id"] == "*7"


# ─── Generic template path: create-if-missing for named set commands ────────


def test_template_set_creates_named_item_when_missing():
    # Fresh router: applying a template whose first command is
    # /ip/hotspot/profile/set name=hsprof1 ... must create the profile rather than
    # failing — /.../set is item-based and needs a .id (the original bug).
    api = MikroTikAPIService()
    runner = FakeRunner({("/ip/hotspot/profile", "print"): []})
    data = {"commands": [
        {"path": "/ip/hotspot/profile", "command": "set",
         "params": {"name": "hsprof1", "use-radius": "yes", "nas-port-type": "wireless-802.11"}},
    ]}
    api._sync_execute_template_commands(runner, data)

    assert not runner.sets("/ip/hotspot/profile")  # no .id-less set went out
    prof_add = runner.adds("/ip/hotspot/profile")[0]["params"]
    assert prof_add["name"] == "hsprof1"  # name kept so the created item is identifiable
    assert prof_add["use-radius"] == "yes"
    assert prof_add["nas-port-type"] == "wireless-802.11"


def test_template_set_resolves_existing_named_item_to_id():
    # When the item already exists, we still resolve name -> .id and set (no add).
    api = MikroTikAPIService()
    runner = FakeRunner({("/ip/hotspot/profile", "print"): [{".id": "*3", "name": "hsprof1"}]})
    data = {"commands": [
        {"path": "/ip/hotspot/profile", "command": "set",
         "params": {"name": "hsprof1", "use-radius": "yes"}},
    ]}
    api._sync_execute_template_commands(runner, data)

    assert not runner.adds("/ip/hotspot/profile")
    prof_set = runner.sets("/ip/hotspot/profile")[0]["params"]
    assert prof_set[".id"] == "*3"
    assert "name" not in prof_set  # redundant once we have the .id
    assert prof_set["use-radius"] == "yes"


def test_template_set_still_raises_for_uncreatable_named_item():
    # Paths outside the create-if-missing allow-list (e.g. physical interfaces that
    # can't be added) keep raising — a missing item there is a real error.
    api = MikroTikAPIService()
    runner = FakeRunner({("/interface/ethernet", "print"): []})
    data = {"commands": [
        {"path": "/interface/ethernet", "command": "set",
         "params": {"name": "ether1", "comment": "WAN"}},
    ]}
    with pytest.raises(MikroTikOperationError):
        api._sync_execute_template_commands(runner, data)
    assert not runner.adds("/interface/ethernet")


# ─── Section 3: RADIUS apply + secret safety ────────────────────────────────


def test_radius_apply_adds_entry_and_verifies():
    radius_rows = []
    runner = FakeRunner({
        ("/radius", "print"): radius_rows,
        ("/ip/hotspot/profile", "print"): [{".id": "*1", "name": "hsprof1"}],
    })
    # Make the post-apply verify print see the address.
    runner.responses[("/radius", "print")] = [{"address": "203.0.113.10", "service": "hotspot"}]
    data = {"radius_host": "203.0.113.10", "radius_secret": "supersecret", "auth_port": 1812,
            "accounting_port": 1813, "service": "hotspot", "timeout": 3000}
    msg = svc._op_apply_radius(runner, data)
    assert "RADIUS configured" in msg
    add = runner.adds("/radius")[0]["params"]
    assert add["address"] == "203.0.113.10"
    assert add["secret"] == "supersecret"  # passed to RouterOS, server-side only


def test_radius_apply_raises_when_verification_fails():
    runner = FakeRunner({
        ("/radius", "print"): [],  # entry never shows up
        ("/ip/hotspot/profile", "print"): [],
    })
    data = {"radius_host": "203.0.113.10", "radius_secret": "x", "service": "hotspot", "timeout": 3000}
    with pytest.raises(MikroTikOperationError):
        svc._op_apply_radius(runner, data)


def test_command_log_sanitizes_secret():
    # The command-log sanitiser masks any param whose key contains 'secret'.
    masked = _sanitize_mapping({"address": "203.0.113.10", "secret": "supersecret"})
    assert masked["secret"] == "***"
    assert masked["address"] == "203.0.113.10"


# ─── Section 4: NAT & firewall ──────────────────────────────────────────────


def test_nat_apply_adds_masquerade_and_filters():
    runner = FakeRunner({("/ip/firewall/nat", "print"): [], ("/ip/firewall/filter", "print"): []})
    data = {"wan_interface": "ether1", "hotspot_network": "192.168.10.0/24", "enable_nat": True,
            "firewall_options": ["established", "invalid", "icmp"]}
    svc._op_apply_nat(runner, data)
    nat = runner.adds("/ip/firewall/nat")[0]["params"]
    assert nat["action"] == "masquerade" and nat["out-interface"] == "ether1"
    comments = {c["params"]["comment"] for c in runner.adds("/ip/firewall/filter")}
    assert comments == {"allow-established", "drop-invalid", "allow-icmp"}


def test_nat_apply_rejects_wireguard_wan():
    runner = FakeRunner()
    data = {"wan_interface": "wg-hotspot", "hotspot_network": "192.168.10.0/24", "enable_nat": True, "firewall_options": []}
    with pytest.raises(MikroTikOperationError):
        svc._op_apply_nat(runner, data)


def test_nat_apply_skips_duplicate_masquerade():
    runner = FakeRunner({("/ip/firewall/nat", "print"): [{".id": "*1", "comment": "hotspot-nat"}], ("/ip/firewall/filter", "print"): []})
    data = {"wan_interface": "ether1", "hotspot_network": "192.168.10.0/24", "enable_nat": True, "firewall_options": []}
    svc._op_apply_nat(runner, data)
    assert runner.adds("/ip/firewall/nat") == []


def test_nat_detect_excludes_wireguard_from_wan():
    runner = FakeRunner({
        ("/ip/firewall/nat", "print"): [],
        ("/ip/firewall/filter", "print"): [],
        ("/interface", "print"): [{"name": "ether1", "type": "ether"}, {"name": "wg-hotspot", "type": "wg"}],
        ("/ip/route", "print"): [{"dst-address": "0.0.0.0/0", "interface": "ether1"}],
    })
    detected = svc._op_detect_nat(runner, {})
    names = {i["name"] for i in detected["interfaces"]}
    assert "wg-hotspot" not in names
    assert detected["suggested_wan"] == "ether1"


# ─── Status derivation ──────────────────────────────────────────────────────


def test_derive_statuses():
    assert svc.derive_network_status({"bridges": [{"name": "bridge-hotspot"}], "addresses": [{"interface": "bridge-hotspot"}], "dhcp_servers": [{"interface": "bridge-hotspot"}]}) == "configured"
    assert svc.derive_network_status({"bridges": [{"name": "bridge-hotspot"}], "addresses": [], "dhcp_servers": []}) == "partial"
    assert svc.derive_network_status({"bridges": [], "addresses": [], "dhcp_servers": []}) == "unconfigured"
    assert svc.derive_radius_status({"entries": [{"address": "203.0.113.10"}]}, "203.0.113.10") == "configured"
    assert svc.derive_radius_status({"entries": [{"address": "1.1.1.1"}]}, "203.0.113.10") == "partial"
    assert svc.derive_nat_status({"masquerade_count": 1}) == "configured"


# ─── Terminal command generation ────────────────────────────────────────────


def test_network_terminal_commands_match_form():
    lines = svc.network_terminal_commands(_network_data())
    assert any(l.startswith("/interface/bridge/add name=bridge-hotspot") for l in lines)
    assert "/ip/address/add address=192.168.10.1/24 interface=bridge-hotspot" in lines
    assert any("ranges=192.168.10.2-192.168.10.254" in l for l in lines)


def test_radius_terminal_commands_never_embed_secret():
    lines = svc.radius_terminal_commands({"radius_host": "203.0.113.10", "timeout": 3000})
    joined = "\n".join(lines)
    assert "<your-router-secret>" in joined
    assert "supersecret" not in joined


# ─── Live HTTP endpoints ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def setup_env():
    op_a, op_b, token_a, token_b = _create_operator_pair("setup")
    net = _create_network(token_a, "setup")
    return {"token_a": token_a, "token_b": token_b, "router_id": net["router"]["id"]}


def test_status_initial_is_unconfigured_and_offline(setup_env):
    status, body = _request("GET", f"/api/v1/admin/routers/{setup_env['router_id']}/setup/status", token=setup_env["token_a"])
    assert status == 200, body
    assert body["online"] is False
    assert body["sections_complete"] == 0
    for section in ("network", "hotspot", "radius", "nat"):
        assert body[section]["status"] == "unconfigured"


def test_nas_secret_is_masked_never_plaintext(setup_env):
    status, body = _request("GET", f"/api/v1/admin/routers/{setup_env['router_id']}/nas-secret", token=setup_env["token_a"])
    assert status == 200, body
    assert body["masked"] == "●" * 8
    # Created with nas_secret="radius_secret" → hint ends in 'ret', plaintext never returned.
    assert body["hint"].endswith("ret")
    assert "radius_secret" not in str(body)


def test_apply_blocked_when_router_offline(setup_env):
    status, body = _request(
        "POST",
        f"/api/v1/admin/routers/{setup_env['router_id']}/setup/network/apply",
        token=setup_env["token_a"],
        body={"gateway_ip": "192.168.10.1", "prefix": 24, "interfaces": ["ether2"]},
    )
    assert status == 409, body
    assert "connected" in body["detail"].lower()


def test_setup_is_tenant_isolated(setup_env):
    # Operator B must not see operator A's router setup.
    status, body = _request("GET", f"/api/v1/admin/routers/{setup_env['router_id']}/setup/status", token=setup_env["token_b"])
    assert status == 404, body


def test_radius_secret_endpoint_requires_auth(setup_env):
    status, _ = _request("GET", f"/api/v1/admin/routers/{setup_env['router_id']}/nas-secret")
    assert status in (401, 403)
