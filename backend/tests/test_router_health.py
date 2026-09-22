"""Router health: snapshot assembly, threshold evaluation, response shaping.

The snapshot is assembled from real RouterOS `print` output shapes, captured
from the live fleet — including the two cases that are NORMAL here rather than
edge cases:

  * /system/routerboard raises "no such command prefix" on CHR/x86, which is
    what these routers are;
  * /system/health returns {"state": "disabled"} with no sensor keys on a board
    with no sensors.

Either one raising out of the collector would cost the whole poll, so both are
asserted to degrade instead.

Pure unit tests: no server, no DB, no router. Defines neither BASE_URL nor
_request, so tests/conftest.py classifies this module as unit-only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.modules.mikrotik import health as health_service
from src.modules.mikrotik.api_service import HEALTH_SCHEMA_VERSION, MikroTikAPIService

# Captured verbatim from Aflao-net (10.100.0.3), RouterOS 7.23.1 on CHR.
RESOURCE_ROW = {
    "uptime": "3h14m54s", "version": "7.23.1 (stable)", "free-memory": "12656640",
    "total-memory": "268435456", "cpu-load": "1", "free-hdd-space": "69611520",
    "total-hdd-space": "93564928", "architecture-name": "x86_64",
    "board-name": "CHR VMware, Inc. VMware Virtual Platform",
}
POOL_ROWS = [{"id": "*1", "name": "hs-pool", "ranges": "192.168.10.2-192.168.10.62",
              "total": "61", "used": "0", "available": "61"}]
INTERFACE_ROWS = [
    {"name": "ether1", "type": "ether", "running": "true", "disabled": "false",
     "link-downs": "7", "last-link-down-time": "2026-09-22 20:42:21"},
    {"name": "ether2", "type": "ether", "running": "true", "disabled": "false", "link-downs": "0"},
]
SENSORLESS_HEALTH_ROWS = [{"state": "disabled", "state-after-reboot": "enabled"}]


class FakeRunner:
    """Serves canned rows per path; raises for paths marked as errors."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def execute(self, path, command, params=None, queries=None):
        self.calls.append(path)
        value = self.responses.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RuntimeError(f'no such command prefix: {path}')
        return value


def _runner(**overrides):
    responses = {
        "/system/resource": [RESOURCE_ROW],
        "/system/health": SENSORLESS_HEALTH_ROWS,
        "/ip/pool": POOL_ROWS,
        "/interface": INTERFACE_ROWS,
        "/ip/hotspot/active": [],
        "/system/routerboard": RuntimeError("no such command prefix"),
    }
    responses.update(overrides)
    return FakeRunner(responses)


def _build(**overrides):
    svc = MikroTikAPIService()
    runner = _runner(**overrides)
    system_info = svc._sync_get_system_info(runner, {})
    return svc._sync_build_health(runner, system_info), runner


# --------------------------------------------------------------------------
# Snapshot assembly
# --------------------------------------------------------------------------


def test_disk_comes_from_the_resource_row_already_being_fetched():
    health, runner = _build()
    # 93564928 - 69611520 = 23953408 used → 25%
    assert health["disk"] == {"free_bytes": 69611520, "total_bytes": 93564928, "used_percent": 25}
    # One /system/resource print, not two.
    assert runner.calls.count("/system/resource") == 1


def test_snapshot_is_schema_versioned():
    health, _ = _build()
    assert health["schema"] == HEALTH_SCHEMA_VERSION == 1


def test_sensorless_board_degrades_instead_of_failing():
    health, _ = _build()
    assert health["sensors"] == {
        "supported": False, "reason": "board reports no sensors",
        "temperature_c": None, "voltage_v": None,
    }


def test_board_with_sensors_is_parsed():
    health, _ = _build(**{"/system/health": [{"name": "temperature", "value": "46"},
                                             {"name": "voltage", "value": "24.1"}]})
    assert health["sensors"]["supported"] is True
    assert health["sensors"]["temperature_c"] == 46.0
    assert health["sensors"]["voltage_v"] == 24.1


def test_flat_key_sensor_shape_is_also_parsed():
    """Some boards/versions return flat keys rather than name/value pairs."""
    health, _ = _build(**{"/system/health": [{"temperature": "51C", "voltage": "12.1V"}]})
    assert health["sensors"]["temperature_c"] == 51.0
    assert health["sensors"]["voltage_v"] == 12.1


def test_routerboard_absent_on_chr_is_not_an_error():
    health, _ = _build()
    assert health["firmware"] == {"supported": False, "current": None, "available": None}
    # Deliberately NOT recorded in errors — this is expected on x86/CHR.
    assert health["errors"] == []


def test_routerboard_present_is_reported():
    health, _ = _build(**{"/system/routerboard": [
        {"current-firmware": "7.16.2", "upgrade-firmware": "7.23.1"}]})
    assert health["firmware"] == {"supported": True, "current": "7.16.2", "available": "7.23.1"}


def test_pool_uses_routeros_own_totals_not_a_lease_count():
    health, runner = _build()
    assert health["pool"] == [{"name": "hs-pool", "total": 61, "used": 0,
                               "available": 61, "used_percent": 0}]
    # The whole point: no /ip/dhcp-server/lease round-trip.
    assert "/ip/dhcp-server/lease" not in runner.calls


def test_interfaces_capture_routeros_own_flap_counter():
    health, _ = _build()
    ether1 = next(i for i in health["interfaces"] if i["name"] == "ether1")
    assert ether1 == {"name": "ether1", "running": True, "disabled": False,
                      "link_downs": 7, "last_link_down": "2026-09-22 20:42:21"}


def test_one_failing_path_does_not_lose_the_rest():
    health, _ = _build(**{"/ip/pool": RuntimeError("boom")})
    assert health["pool"] == []
    assert any("pool:" in e for e in health["errors"])
    # Everything else still collected.
    assert health["disk"]["used_percent"] == 25
    assert health["interfaces"]


def test_snapshot_costs_six_prints_on_one_runner():
    """One connection; the health reads ride on it rather than adding another."""
    svc = MikroTikAPIService()
    runner = _runner()
    result = svc._sync_collect_metrics_snapshot(runner, {})
    system_info, active_users, health = result
    assert system_info.ros_version == "7.23.1 (stable)"
    assert active_users == []
    assert health["schema"] == 1
    assert runner.calls == ["/system/resource", "/ip/hotspot/active", "/system/health",
                            "/ip/pool", "/interface", "/system/routerboard"]


# --------------------------------------------------------------------------
# Threshold evaluation
# --------------------------------------------------------------------------


def _health(disk_pct=25, pool_pct=0, interfaces=None):
    return {
        "schema": 1,
        "disk": {"free_bytes": 1, "total_bytes": 2, "used_percent": disk_pct},
        "sensors": {"supported": False, "reason": "board reports no sensors",
                    "temperature_c": None, "voltage_v": None},
        "pool": [{"name": "hs-pool", "total": 61, "used": 0, "available": 61,
                  "used_percent": pool_pct}],
        "interfaces": interfaces if interfaces is not None else [
            {"name": "ether1", "running": True, "disabled": False, "link_downs": 7,
             "last_link_down": "2026-09-22 20:42:21"}],
        "firmware": {"supported": False, "current": None, "available": None},
        "errors": [],
    }


def test_healthy_snapshot_produces_no_warnings():
    assert health_service.evaluate(_health()) == {"overall": "ok", "warnings": []}


def test_missing_snapshot_is_unknown_not_ok():
    """Absence of data is not health."""
    assert health_service.evaluate(None)["overall"] == "unknown"


@pytest.mark.parametrize("pct,expected", [(84, "ok"), (85, "warning"), (99, "warning")])
def test_disk_threshold(pct, expected):
    assert health_service.evaluate(_health(disk_pct=pct))["overall"] == expected


@pytest.mark.parametrize("pct,expected", [(89, "ok"), (90, "warning")])
def test_pool_threshold(pct, expected):
    assert health_service.evaluate(_health(pool_pct=pct))["overall"] == expected


def test_no_cpu_or_memory_warning_even_at_extremes():
    """A live CHR here idles at 95% memory used — thresholds on CPU/memory would
    fire permanently from day one, so they are deliberately absent."""
    verdict = health_service.evaluate(_health())
    assert all(w["key"] not in ("cpu", "memory") for w in verdict["warnings"])


def test_interface_down_warns():
    verdict = health_service.evaluate(_health(interfaces=[
        {"name": "ether1", "running": False, "disabled": False, "link_downs": 7}]))
    assert verdict["overall"] == "warning"
    assert verdict["warnings"][0]["key"] == "link_down"


def test_disabled_interface_is_not_a_warning():
    verdict = health_service.evaluate(_health(interfaces=[
        {"name": "ether5", "running": False, "disabled": True, "link_downs": 0}]))
    assert verdict["overall"] == "ok"


def test_link_flap_needs_the_previous_snapshot():
    """The real case from the live fleet: ether1 dropped and came back between
    polls, so `running` is true and only the counter reveals it."""
    previous = _health(interfaces=[
        {"name": "ether1", "running": True, "disabled": False, "link_downs": 4}])
    current = _health(interfaces=[
        {"name": "ether1", "running": True, "disabled": False, "link_downs": 7,
         "last_link_down": "2026-09-22 20:42:21"}])

    # Without the previous snapshot there is nothing to compare — no warning.
    assert health_service.evaluate(current)["overall"] == "ok"

    verdict = health_service.evaluate(current, previous)
    assert verdict["overall"] == "warning"
    warning = verdict["warnings"][0]
    assert warning["key"] == "link_flap"
    assert "3 time(s)" in warning["detail"]
    assert "2026-09-22 20:42:21" in warning["detail"]


def test_unchanged_flap_counter_does_not_warn():
    snapshot = _health()
    assert health_service.evaluate(snapshot, snapshot)["overall"] == "ok"


def test_new_interface_since_last_poll_does_not_warn():
    previous = _health(interfaces=[])
    assert health_service.evaluate(_health(), previous)["overall"] == "ok"


# --------------------------------------------------------------------------
# Response shaping
# --------------------------------------------------------------------------


class _Metric:
    def __init__(self, health_payload):
        self.health = health_payload
        self.collected_at = datetime(2026, 9, 22, 21, 55, tzinfo=timezone.utc)
        self.board_name = "CHR VMware, Inc. VMware Virtual Platform"
        self.ros_version = "7.23.1 (stable)"
        self.uptime_seconds = 11694


def test_response_includes_as_of_and_verdict():
    router_id = str(uuid.uuid4())
    body = health_service.build_response(router_id, _Metric(_health()), None)
    assert body["router_id"] == router_id
    assert body["as_of"] == "2026-09-22T21:55:00+00:00"
    assert body["overall"] == "ok"
    assert body["schema"] == 1
    assert body["ros_version"] == "7.23.1 (stable)"


def test_response_for_a_router_never_polled():
    body = health_service.build_response(str(uuid.uuid4()), None, None)
    assert body["as_of"] is None
    assert body["overall"] == "unknown"
    assert body["pool"] == [] and body["interfaces"] == []
