"""Read-time evaluation of a stored router health snapshot.

The collector stores raw state; the verdicts live here. Two reasons:

  * thresholds can be retuned without re-collecting, and every already-stored
    snapshot is re-evaluated under the new ones for free;
  * link instability is only visible by comparing two consecutive snapshots,
    which is a read-time operation over the 24h of rows we already keep.

Thresholds are deliberately narrow. Disk, DHCP pool and link flaps are the
three that produce a real, actionable "customers are affected" signal on this
fleet. CPU and memory are charted but NOT warned on: a live production router
here idles at 95% memory used (normal for CHR), so a naive threshold would fire
permanently from day one. They get thresholds once there is a real baseline
across boards.
"""
from __future__ import annotations

from typing import Any

# Above this share of the disk used, the router is close to being unable to
# write hotspot files, logs or packages.
DISK_USED_WARN_PERCENT = 85
# A DHCP pool this full means new customers are about to stop getting an
# address — the failure that looks like "the internet is down" on a router
# whose every other indicator is green.
POOL_USED_WARN_PERCENT = 90


def _warning(key: str, title: str, detail: str, severity: str = "warning") -> dict[str, Any]:
    return {"key": key, "severity": severity, "title": title, "detail": detail}


def evaluate(current: dict[str, Any] | None, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return {"overall", "warnings"} for a stored snapshot.

    `current` None (never collected, or the health leg failed on that poll)
    yields "unknown" rather than "ok" — absence of data is not health.
    """
    if not current:
        return {"overall": "unknown", "warnings": []}

    warnings: list[dict[str, Any]] = []

    disk = current.get("disk") or {}
    used = disk.get("used_percent")
    if isinstance(used, int) and used >= DISK_USED_WARN_PERCENT:
        warnings.append(_warning(
            "disk",
            "Disk almost full",
            f"{used}% of storage is in use. The router may be unable to write "
            f"hotspot pages, logs or upgrades.",
        ))

    for pool in current.get("pool") or []:
        pool_used = pool.get("used_percent")
        if isinstance(pool_used, int) and pool_used >= POOL_USED_WARN_PERCENT:
            warnings.append(_warning(
                "pool",
                "DHCP pool nearly exhausted",
                f"{pool.get('name')} is {pool_used}% allocated "
                f"({pool.get('used')} of {pool.get('total')}). New customers will "
                f"stop receiving an IP address once it fills.",
            ))

    previous_downs = {
        iface.get("name"): iface.get("link_downs")
        for iface in ((previous or {}).get("interfaces") or [])
    }
    for iface in current.get("interfaces") or []:
        name = iface.get("name")
        if iface.get("disabled"):
            continue
        if not iface.get("running"):
            warnings.append(_warning(
                "link_down",
                "Interface down",
                f"{name} is not running. Anything behind it is offline.",
            ))
            continue
        # Flap detection: RouterOS keeps the counter, so a rise between two
        # consecutive samples is a drop that self-recovered — invisible to any
        # check that only looks at the current `running` flag.
        before = previous_downs.get(name)
        now = iface.get("link_downs")
        if isinstance(before, int) and isinstance(now, int) and now > before:
            last = iface.get("last_link_down")
            warnings.append(_warning(
                "link_flap",
                "Link unstable",
                f"{name} dropped {now - before} time(s) since the last check"
                + (f" — last at {last}" if last else "")
                + ". It is up now, but customers lost connectivity on each drop.",
            ))

    return {"overall": "warning" if warnings else "ok", "warnings": warnings}


def build_response(router_id: str, metric, previous_health: dict[str, Any] | None) -> dict[str, Any]:
    """Shape the router-detail Health tab consumes.

    `metric` may be None (no poll has ever succeeded for this router) — the tab
    renders that as "not collected yet", which is honestly different from "all
    clear".
    """
    health = (metric.health if metric else None) or None
    verdict = evaluate(health, previous_health)
    return {
        "router_id": router_id,
        "as_of": metric.collected_at.isoformat() if metric else None,
        "schema": (health or {}).get("schema"),
        "overall": verdict["overall"],
        "warnings": verdict["warnings"],
        "disk": (health or {}).get("disk"),
        "sensors": (health or {}).get("sensors"),
        "pool": (health or {}).get("pool") or [],
        "interfaces": (health or {}).get("interfaces") or [],
        "firmware": (health or {}).get("firmware"),
        "errors": (health or {}).get("errors") or [],
        "board_name": metric.board_name if metric else None,
        "ros_version": metric.ros_version if metric else None,
        "uptime_seconds": metric.uptime_seconds if metric else None,
    }
