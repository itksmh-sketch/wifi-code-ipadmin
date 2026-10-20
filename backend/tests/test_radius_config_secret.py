"""The router's RADIUS shared secret: never serialized out of RadiusConfig (the
router diagnostics response is built from it), still sent to the router by both
paths that write a /radius entry (the setup wizard and the provisioner), and
masked in their command logs.

No router and no database: the RouterOS API is a fake that records what it is
sent, behind the real SyncCommandRunner, so the arguments checked are the ones
the router would receive.
"""
from __future__ import annotations

import json

from src.modules.mikrotik import setup_service
from src.modules.mikrotik.api_service import MikroTikAPIService, SyncCommandRunner
from src.modules.mikrotik.types import DiagnosticCheck, RadiusConfig

SECRET = "S3cret-RADIUS-XYZZY"


class FakeResource:
    def __init__(self, api, path):
        self.api, self.path = api, path

    def call(self, command, arguments=None, queries=None):
        arguments = dict(arguments or {})
        self.api.calls.append((self.path, command, arguments))
        rows = self.api.state.setdefault(self.path, [])
        if command == "print":
            return [dict(r) for r in rows]
        if command == "add":
            rows.append({"id": f"*{len(rows) + 1}", **arguments})
            return []
        if command == "set":
            if ".id" in arguments:  # an item; a singleton (/radius/incoming) has no id
                row = next(r for r in rows if r["id"] == arguments[".id"])
                row.update({k: v for k, v in arguments.items() if k != ".id"})
            return []
        if command == "remove":
            self.api.state[self.path] = [r for r in rows if r["id"] != arguments[".id"]]
            return []
        return []


class FakeApi:
    def __init__(self, state=None):
        self.state = state or {}
        self.calls: list[tuple[str, str, dict]] = []

    def get_resource(self, path):
        return FakeResource(self, path)


def radius_writes(api):
    return [(cmd, args) for path, cmd, args in api.calls if path == "/radius" and cmd in ("add", "set")]


# ── never serialized ──────────────────────────────────────────────────────

def test_secret_is_never_serialized():
    cfg = RadiusConfig(id="*1", service="hotspot", address="10.100.0.1", secret=SECRET, secret_configured=True)
    assert cfg.secret == SECRET  # still there for server-side use
    dumped = cfg.model_dump()
    assert "secret" not in dumped and dumped["secret_configured"] is True
    for text in (json.dumps(dumped), cfg.model_dump_json(), repr(cfg), str(cfg),
                 json.dumps(cfg.model_dump(mode="json")), json.dumps(cfg.model_dump(include={"secret", "address"}))):
        assert SECRET not in text


def test_diagnostics_payload_built_from_it_carries_no_secret():
    cfg = RadiusConfig(address="10.100.0.1", secret=SECRET)
    check = DiagnosticCheck(name="radius_config", status="pass", message="ok",
                            data={"radius_entries": [cfg.model_dump()]})
    assert SECRET not in check.model_dump_json()


def test_secret_configured_reports_what_the_router_returned():
    svc = MikroTikAPIService()
    api = FakeApi({"/radius": [
        {"id": "*1", "service": "hotspot", "address": "10.100.0.1", "secret": SECRET},
        {"id": "*2", "service": "hotspot", "address": "10.100.0.1", "secret": ""},
        {"id": "*3", "service": "hotspot", "address": "10.100.0.1"},  # an API user without 'sensitive'
    ]})
    configs = svc._sync_get_radius_config(SyncCommandRunner(api), {})
    assert [c.secret_configured for c in configs] == [True, False, None]
    assert configs[0].secret == SECRET


# ── still sent to the router ──────────────────────────────────────────────

def _wizard_data(**overrides):
    return {"radius_host": "10.100.0.1", "radius_secret": SECRET, "auth_port": 1812, "accounting_port": 1813,
            "service": "hotspot", "timeout": 3000, **overrides}


def test_wizard_radius_apply_sends_the_secret_on_a_new_entry():
    api = FakeApi()
    runner = SyncCommandRunner(api)
    assert setup_service._op_apply_radius(runner, _wizard_data()) == "RADIUS configured"
    assert radius_writes(api) == [("add", {
        "service": "hotspot", "address": "10.100.0.1", "secret": SECRET,
        "authentication-port": "1812", "accounting-port": "1813", "timeout": "3s",
    })]
    # The command log (stored in router_provision_log) masks it.
    logged = [c for c in runner.commands if c["path"] == "/radius" and c["command"] == "add"]
    assert logged and logged[0]["params"]["secret"] == "***"
    assert SECRET not in json.dumps(runner.commands, default=str)


def test_wizard_radius_apply_sends_the_secret_when_updating_an_entry():
    api = FakeApi({"/radius": [{"id": "*1", "service": "hotspot", "address": "34.122.11.114", "secret": "old"}]})
    runner = SyncCommandRunner(api)
    setup_service._op_apply_radius(runner, _wizard_data())
    (command, args), = radius_writes(api)
    assert command == "set" and args[".id"] == "*1" and args["secret"] == SECRET and args["address"] == "10.100.0.1"
    assert api.state["/radius"][0]["secret"] == SECRET
    assert SECRET not in json.dumps(runner.commands, default=str)


def test_provisioner_radius_write_sends_the_secret():
    svc = MikroTikAPIService()
    data = {"radius_host": "10.100.0.1", "radius_secret": SECRET, "auth_port": 1812, "accounting_port": 1813,
            "service": "hotspot"}
    for state in ({}, {"/radius": [{"id": "*1", "service": "hotspot", "address": "10.100.0.1", "secret": "old"}]}):
        api = FakeApi(state)
        runner = SyncCommandRunner(api)
        svc._sync_set_radius_server(runner, data)
        (command, args), = radius_writes(api)
        assert command == ("set" if state else "add") and args["secret"] == SECRET
        assert SECRET not in json.dumps(runner.commands, default=str)
