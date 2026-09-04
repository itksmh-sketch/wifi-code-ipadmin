"""Read-only service health probes for the platform-owner monitor.

Deliberately privilege-free: every probe below uses network reach the backend
already has (host networking puts it on the same loopback as Postgres,
PgBouncer, Redis, wg-manager and the primary FreeRADIUS) or a credential the
app already holds. Nothing here talks to the Docker daemon — container control
is a root-equivalent surface and a status display is not worth crossing it for.

Every probe is bounded by `settings.health_check_timeout_seconds` and they all
run concurrently, so one hung service reports "down" quickly instead of
stalling the whole endpoint. Blocking socket work is pushed to a thread so it
can never block the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import socket
import struct
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import text

from src.config import get_settings
from src.db.base import async_session_factory

# Status vocabulary shared with the portal's badge classes.
UP = "up"
DEGRADED = "degraded"
DOWN = "down"

# RADIUS packet codes we expect back from a Status-Server probe: the auth port
# answers Access-Accept (2), the accounting port Accounting-Response (5).
_RADIUS_STATUS_SERVER = 12
_RADIUS_ACCESS_ACCEPT = 2
_RADIUS_ACCOUNTING_RESPONSE = 5

# "Sep-04 11:07:16 j_complete=39925 j_failed=0 j_retried=0 j_ongoing=0 queued=0"
_ARQ_HEALTH_RE = re.compile(r"^(?P<ts>\w{3}-\d{2} \d{2}:\d{2}:\d{2})\s+(?P<counters>.*)$")
_ARQ_HEALTH_KEY = "arq:queue:health-check"


def _result(
    key: str,
    name: str,
    status: str,
    detail: str,
    *,
    group: str,
    latency_ms: float | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "group": group,
        "status": status,
        "detail": detail,
        "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        "meta": meta or {},
    }


# --------------------------------------------------------------------------
# RADIUS Status-Server
# --------------------------------------------------------------------------

def _status_server_probe(host: str, port: int, secret: str, timeout: float) -> tuple[int | None, float, str | None]:
    """Send one RADIUS Status-Server packet and return (code, rtt_ms, error).

    Proves the daemon is *processing requests*, which a bare port check cannot.
    The Message-Authenticator is always included: FreeRADIUS requires it on
    Status-Server regardless of the client's require_message_authenticator.
    """
    ident = os.urandom(1)[0]
    authenticator = os.urandom(16)
    # Message-Authenticator (type 80, length 18) zeroed for the HMAC, then filled in.
    blank_ma = b"\x50\x12" + b"\x00" * 16
    header = struct.pack("!BBH", _RADIUS_STATUS_SERVER, ident, 20 + len(blank_ma))
    packet = header + authenticator + blank_ma
    digest = hmac.new(secret.encode(), packet, hashlib.md5).digest()
    packet = packet[:20] + b"\x50\x12" + digest

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    started = time.monotonic()
    try:
        sock.sendto(packet, (host, port))
        while True:
            data, _ = sock.recvfrom(4096)
            # Ignore stray datagrams that aren't the reply to our request id.
            if len(data) >= 2 and data[1] == ident:
                return data[0], (time.monotonic() - started) * 1000, None
    except socket.timeout:
        return None, (time.monotonic() - started) * 1000, "no response (timeout)"
    except Exception as exc:  # noqa: BLE001 - any failure is simply "down"
        return None, (time.monotonic() - started) * 1000, f"{type(exc).__name__}: {exc}"
    finally:
        sock.close()


async def _check_freeradius(key: str, name: str, auth_port: int, acct_port: int) -> dict[str, Any]:
    settings = get_settings()
    timeout = settings.health_check_timeout_seconds
    secret = settings.radius_status_secret

    auth_task = asyncio.to_thread(_status_server_probe, "127.0.0.1", auth_port, secret, timeout)
    acct_task = asyncio.to_thread(_status_server_probe, "127.0.0.1", acct_port, secret, timeout)
    (auth_code, auth_ms, auth_err), (acct_code, acct_ms, acct_err) = await asyncio.gather(auth_task, acct_task)

    auth_ok = auth_code == _RADIUS_ACCESS_ACCEPT
    acct_ok = acct_code == _RADIUS_ACCOUNTING_RESPONSE
    meta = {
        "auth_port": auth_port,
        "acct_port": acct_port,
        "auth_ok": auth_ok,
        "acct_ok": acct_ok,
    }

    if auth_ok and acct_ok:
        return _result(key, name, UP, f"auth + accounting responding (:{auth_port}, :{acct_port})",
                       group="radius", latency_ms=max(auth_ms, acct_ms), meta=meta)
    if not auth_ok and not acct_ok:
        reason = auth_err or (f"unexpected code {auth_code}" if auth_code is not None else "no response")
        return _result(key, name, DOWN, f"no Status-Server response on :{auth_port} or :{acct_port} — {reason}",
                       group="radius", latency_ms=max(auth_ms, acct_ms), meta=meta)
    if auth_ok:
        reason = acct_err or f"unexpected code {acct_code}"
        return _result(key, name, DEGRADED, f"auth OK but accounting :{acct_port} not responding — {reason}",
                       group="radius", latency_ms=acct_ms, meta=meta)
    reason = auth_err or f"unexpected code {auth_code}"
    return _result(key, name, DEGRADED, f"accounting OK but auth :{auth_port} not responding — {reason}",
                   group="radius", latency_ms=auth_ms, meta=meta)


# --------------------------------------------------------------------------
# Datastores
# --------------------------------------------------------------------------

async def _check_pgbouncer() -> dict[str, Any]:
    """`SELECT 1` over the app's real engine, which points at PgBouncer.

    Covers the whole app query path: a success proves PgBouncer is pooling AND
    Postgres is answering queries behind it.

    Uses its own short-lived session rather than the request's. A probe that
    times out leaves its connection invalidated, and that must not contaminate
    the session the request itself is using.
    """
    settings = get_settings()
    started = time.monotonic()
    try:
        async with async_session_factory() as session:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")), timeout=settings.health_check_timeout_seconds
            )
        elapsed = (time.monotonic() - started) * 1000
        return _result("pgbouncer", "PgBouncer", UP, "SELECT 1 through the pool succeeded",
                       group="data", latency_ms=elapsed, meta={"port": 6432, "query_ok": True})
    except asyncio.TimeoutError:
        elapsed = (time.monotonic() - started) * 1000
        return _result("pgbouncer", "PgBouncer", DOWN,
                       f"SELECT 1 timed out after {settings.health_check_timeout_seconds}s",
                       group="data", latency_ms=elapsed, meta={"port": 6432, "query_ok": False})
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.monotonic() - started) * 1000
        return _result("pgbouncer", "PgBouncer", DOWN, f"{type(exc).__name__}: {exc}",
                       group="data", latency_ms=elapsed, meta={"port": 6432, "query_ok": False})


def _tcp_probe(host: str, port: int, timeout: float) -> tuple[bool, float, str | None]:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.monotonic() - started) * 1000, None
    except Exception as exc:  # noqa: BLE001
        return False, (time.monotonic() - started) * 1000, f"{type(exc).__name__}: {exc}"


async def _check_postgres_direct() -> dict[str, Any]:
    """TCP reach to Postgres on 5432 — the port FreeRADIUS uses directly.

    Deliberately a listener check rather than a second authenticated
    connection: the query path is already proven end-to-end by the PgBouncer
    probe, and this avoids re-deriving DB credentials for a status display.
    """
    settings = get_settings()
    port = settings.postgres_direct_port
    ok, elapsed, err = await asyncio.to_thread(
        _tcp_probe, "127.0.0.1", port, settings.health_check_timeout_seconds
    )
    if ok:
        return _result("postgres", "PostgreSQL", UP, f"listener accepting connections on :{port}",
                       group="data", latency_ms=elapsed, meta={"port": port})
    return _result("postgres", "PostgreSQL", DOWN, f"cannot reach :{port} — {err}",
                   group="data", latency_ms=elapsed, meta={"port": port})


async def _check_redis() -> dict[str, Any]:
    settings = get_settings()
    started = time.monotonic()
    redis: Redis | None = None
    try:
        redis = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=settings.health_check_timeout_seconds,
            socket_timeout=settings.health_check_timeout_seconds,
        )
        pong = await asyncio.wait_for(redis.ping(), timeout=settings.health_check_timeout_seconds)
        elapsed = (time.monotonic() - started) * 1000
        if pong:
            return _result("redis", "Redis", UP, "PING acknowledged", group="data", latency_ms=elapsed)
        return _result("redis", "Redis", DOWN, "PING returned a falsy reply", group="data", latency_ms=elapsed)
    except asyncio.TimeoutError:
        elapsed = (time.monotonic() - started) * 1000
        return _result("redis", "Redis", DOWN,
                       f"PING timed out after {settings.health_check_timeout_seconds}s",
                       group="data", latency_ms=elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.monotonic() - started) * 1000
        return _result("redis", "Redis", DOWN, f"{type(exc).__name__}: {exc}", group="data", latency_ms=elapsed)
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:  # noqa: BLE001 - never let cleanup mask the result
                pass


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------

def _parse_arq_heartbeat(raw: str) -> tuple[datetime | None, dict[str, str]]:
    """Parse arq's health-check string into (timestamp, counters).

    arq writes the timestamp without a year, so it is reconstructed against the
    current UTC year, stepping back one year if that lands in the future
    (a heartbeat written on Dec 31 and read on Jan 1).
    """
    match = _ARQ_HEALTH_RE.match(raw.strip())
    if not match:
        return None, {}
    counters: dict[str, str] = {}
    for token in match.group("counters").split():
        if "=" in token:
            field, _, value = token.partition("=")
            counters[field] = value
    now = datetime.now(timezone.utc)
    try:
        parsed = datetime.strptime(match.group("ts"), "%b-%d %H:%M:%S").replace(
            year=now.year, tzinfo=timezone.utc
        )
    except ValueError:
        return None, counters
    if parsed > now + _clock_skew_allowance():
        parsed = parsed.replace(year=now.year - 1)
    return parsed, counters


def _clock_skew_allowance():
    from datetime import timedelta
    return timedelta(minutes=5)


async def _check_worker() -> dict[str, Any]:
    """Read arq's heartbeat key from Redis.

    NOTE: arq's default `health_check_interval` is 3600s, so this heartbeat can
    legitimately be up to an hour stale — a worker that died minutes ago still
    reads as up. The age is therefore surfaced explicitly rather than hidden
    behind a green dot. Lowering `health_check_interval` in WorkerSettings
    would sharpen it, but that requires a worker restart.
    """
    settings = get_settings()
    started = time.monotonic()
    redis: Redis | None = None
    try:
        redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.health_check_timeout_seconds,
            socket_timeout=settings.health_check_timeout_seconds,
        )
        raw = await asyncio.wait_for(
            redis.get(_ARQ_HEALTH_KEY), timeout=settings.health_check_timeout_seconds
        )
        elapsed = (time.monotonic() - started) * 1000
    except asyncio.TimeoutError:
        return _result("worker", "Worker (arq)", DOWN,
                       "could not read heartbeat — Redis timed out", group="app",
                       latency_ms=(time.monotonic() - started) * 1000)
    except Exception as exc:  # noqa: BLE001
        return _result("worker", "Worker (arq)", DOWN,
                       f"could not read heartbeat — {type(exc).__name__}: {exc}", group="app",
                       latency_ms=(time.monotonic() - started) * 1000)
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:  # noqa: BLE001
                pass

    if not raw:
        return _result("worker", "Worker (arq)", DOWN,
                       "no heartbeat key in Redis — worker has not reported for over an hour",
                       group="app", latency_ms=elapsed, meta={"heartbeat_key": _ARQ_HEALTH_KEY})

    beat_at, counters = _parse_arq_heartbeat(raw)
    meta: dict[str, Any] = {"heartbeat_key": _ARQ_HEALTH_KEY, "counters": counters}
    if beat_at is None:
        return _result("worker", "Worker (arq)", DEGRADED,
                       f"heartbeat present but unparseable: {raw[:120]}",
                       group="app", latency_ms=elapsed, meta=meta)

    age = int((datetime.now(timezone.utc) - beat_at).total_seconds())
    meta["heartbeat_at"] = beat_at.isoformat()
    meta["heartbeat_age_seconds"] = age
    summary = " ".join(f"{k}={v}" for k, v in counters.items()) or "no counters"
    age_text = f"{age // 60}m {age % 60}s ago"

    if age > settings.worker_heartbeat_stale_seconds:
        return _result("worker", "Worker (arq)", DOWN,
                       f"heartbeat stale — last beat {age_text} ({summary})",
                       group="app", latency_ms=elapsed, meta=meta)
    return _result("worker", "Worker (arq)", UP,
                   f"heartbeat {age_text} · {summary}",
                   group="app", latency_ms=elapsed, meta=meta)


# --------------------------------------------------------------------------
# wg-manager / WireGuard interface
# --------------------------------------------------------------------------

async def _check_wg_manager() -> dict[str, Any]:
    """Ask the sidecar's own /health — it reports the wg0 interface state, not
    just that the HTTP listener is up."""
    settings = get_settings()
    url = settings.wg_manager_url.rstrip("/") + "/health"
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=settings.health_check_timeout_seconds) as client:
            response = await client.get(url)
        elapsed = (time.monotonic() - started) * 1000
    except Exception as exc:  # noqa: BLE001
        return _result("wg_manager", "wg-manager / WireGuard", DOWN,
                       f"sidecar unreachable at {url} — {type(exc).__name__}: {exc}",
                       group="network", latency_ms=(time.monotonic() - started) * 1000)

    if response.status_code != 200:
        return _result("wg_manager", "wg-manager / WireGuard", DOWN,
                       f"sidecar returned HTTP {response.status_code}",
                       group="network", latency_ms=elapsed)
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return _result("wg_manager", "wg-manager / WireGuard", DEGRADED,
                       "sidecar responded with a non-JSON body", group="network", latency_ms=elapsed)

    interface = body.get("interface") or settings.wg_interface
    meta = {
        "interface": interface,
        "wg": bool(body.get("wg")),
        "wg_show_ok": bool(body.get("wg_show_ok")),
        "detail": body.get("detail"),
    }
    if body.get("ok"):
        return _result("wg_manager", "wg-manager / WireGuard", UP,
                       f"sidecar healthy · interface {interface} up", group="network",
                       latency_ms=elapsed, meta=meta)
    # HTTP answered but the interface check failed — the sidecar is alive and
    # the tunnel is not. That distinction is the whole point of this row.
    reason = body.get("detail") or f"wg={meta['wg']} wg_show_ok={meta['wg_show_ok']}"
    return _result("wg_manager", "wg-manager / WireGuard", DEGRADED,
                   f"sidecar responding but interface {interface} unhealthy — {reason}",
                   group="network", latency_ms=elapsed, meta=meta)


# --------------------------------------------------------------------------
# Backend (self)
# --------------------------------------------------------------------------

def _process_uptime_seconds() -> int | None:
    """Best-effort process age from /proc. Resets when uvicorn --reload
    respawns the worker, which is itself useful to see."""
    try:
        with open("/proc/uptime", "r") as handle:
            host_uptime = float(handle.read().split()[0])
        with open("/proc/self/stat", "r") as handle:
            stat = handle.read()
        # Field 22 (1-indexed) is starttime in clock ticks; skip comm, which may
        # contain spaces, by slicing after the closing paren.
        fields = stat[stat.rindex(")") + 2:].split()
        starttime_ticks = float(fields[19])
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        return max(0, int(host_uptime - (starttime_ticks / ticks_per_second)))
    except Exception:  # noqa: BLE001 - uptime is a nicety, never a failure
        return None


async def _check_backend() -> dict[str, Any]:
    uptime = _process_uptime_seconds()
    if uptime is None:
        detail = "serving this request"
    else:
        hours, remainder = divmod(uptime, 3600)
        detail = f"serving this request · up {hours}h {remainder // 60}m"
    return _result("backend", "Backend API", UP, detail, group="app",
                   meta={"uptime_seconds": uptime})


# --------------------------------------------------------------------------
# Aggregate
# --------------------------------------------------------------------------

async def collect_service_health() -> dict[str, Any]:
    """Run every probe concurrently and return per-service status.

    A probe that raises is reported as `down` for that service only — one
    broken check never takes down the whole view.
    """
    settings = get_settings()
    checked_at = datetime.now(timezone.utc)
    started = time.monotonic()

    probes: list[tuple[str, str, str, Any]] = [
        ("backend", "Backend API", "app", _check_backend()),
        ("worker", "Worker (arq)", "app", _check_worker()),
        ("pgbouncer", "PgBouncer", "data", _check_pgbouncer()),
        ("postgres", "PostgreSQL", "data", _check_postgres_direct()),
        ("redis", "Redis", "data", _check_redis()),
        (
            "radius_primary",
            "FreeRADIUS primary",
            "radius",
            _check_freeradius("radius_primary", "FreeRADIUS primary",
                              settings.radius_primary_auth_port, settings.radius_primary_acct_port),
        ),
        (
            "radius_secondary",
            "FreeRADIUS secondary",
            "radius",
            _check_freeradius("radius_secondary", "FreeRADIUS secondary",
                              settings.radius_secondary_auth_port, settings.radius_secondary_acct_port),
        ),
        ("wg_manager", "wg-manager / WireGuard", "network", _check_wg_manager()),
    ]

    # Hard ceiling so the endpoint stays responsive even if a probe misbehaves
    # in a way its own timeout does not cover.
    overall_timeout = settings.health_check_timeout_seconds * 3
    outcomes = await asyncio.gather(
        *(asyncio.wait_for(coro, timeout=overall_timeout) for _, _, _, coro in probes),
        return_exceptions=True,
    )

    services: list[dict[str, Any]] = []
    for (key, name, group, _), outcome in zip(probes, outcomes):
        if isinstance(outcome, asyncio.TimeoutError):
            services.append(_result(key, name, DOWN,
                                    f"health probe exceeded {overall_timeout}s", group=group))
        elif isinstance(outcome, BaseException):
            services.append(_result(key, name, DOWN,
                                    f"health probe failed — {type(outcome).__name__}: {outcome}", group=group))
        else:
            services.append(outcome)

    counts = {
        UP: sum(1 for s in services if s["status"] == UP),
        DEGRADED: sum(1 for s in services if s["status"] == DEGRADED),
        DOWN: sum(1 for s in services if s["status"] == DOWN),
    }
    if counts[DOWN]:
        overall = DOWN
    elif counts[DEGRADED]:
        overall = DEGRADED
    else:
        overall = UP

    return {
        "overall": overall,
        "checked_at": checked_at.isoformat(),
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
        "counts": counts,
        "services": services,
    }
