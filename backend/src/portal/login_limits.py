"""Wrong-code limits for the captive portal's POST /portal/authenticate.

A voucher code is a bearer credential, and the portal answers any code it is
given, so guessing is bounded here. Only a code that doesn't exist (for the
router's operator) counts as a failure. A code that exists but is expired,
used up or disabled grants nothing and isn't counted, so a customer retrying
an old code isn't locked out; a numeric code with a wrong check digit is
refused before the lookup and isn't counted either; and a success never uses
up anything.

Counters, in fixed 15-minute windows in Redis (the shared limiter's pattern:
INCR and EXPIRE NX in one MULTI):

  rl:portal-auth-fail:mac:{MAC}      per device: the main limit
  rl:portal-auth-fail:nomac:{ip}     per IP, for requests with no usable MAC
  rl:portal-auth-fail:ip:{ip}        per client IP, i.e. per site's public address
  rl:portal-auth-fail:op:{operator}  per operator: counted, never blocks; feeds the alert

The MAC is a form field the client sends, so a script can send a new one with
every guess: the per-device limit is for honest customers (one phone's typos
don't pause the whole site), and the per-IP limit is the real bound. The
limits are platform settings (migration 062), read at most once a minute.

Alerts, each at most once per operator per hour: "site_blocked" when an IP
reaches its limit, and "guessing_suspected" when the operator's count reaches
the alert threshold (also sent to the platform owner). Each is a log line, an
audit row and an email. Every wrong code also updates that day's row in
portal_login_failure_stats, so the limits can be tuned from real peaks.

If Redis can't be reached the limits fail open (logins work, unlimited), the
fault is logged, and the platform owner gets an alert, at most once an hour
per process.
"""
from __future__ import annotations

import asyncio
import html
import math
import re
import time
import uuid
from dataclasses import dataclass

import structlog
from fastapi import HTTPException
from sqlalchemy import select, text

from src.config import get_settings
from src.db.base import async_session_factory
from src.middleware.rate_limit import get_rate_limit_redis

log = structlog.get_logger("portal.login_limits")

WINDOW_SECONDS = 15 * 60
ALERT_DEDUPE_SECONDS = 60 * 60
LIMITS_CACHE_SECONDS = 60
FAIL_OPEN_LOG_INTERVAL_SECONDS = 60

_PREFIX = "rl:portal-auth-fail"
_ALERT_PREFIX = "rl:portal-auth-alert"

SITE_BLOCKED = "site_blocked"
GUESSING_SUSPECTED = "guessing_suspected"
AUDIT_ACTIONS = {
    SITE_BLOCKED: "portal.login_failures_site_blocked",
    GUESSING_SUSPECTED: "portal.login_failures_guessing",
}
UNAVAILABLE_ACTION = "portal.login_limits_unavailable"

DEVICE_LIMIT_DETAIL = "Too many wrong codes from this device. Try again in {minutes}."
SITE_LIMIT_DETAIL = "Too many wrong codes on this hotspot. Try again in {minutes}."

_MAC_RE = re.compile(r"^[0-9A-F]{2}([:-]?)[0-9A-F]{2}(?:\1[0-9A-F]{2}){4}$")


@dataclass(frozen=True)
class Limits:
    device: int
    ip: int
    operator_alert: int


@dataclass(frozen=True)
class Attempt:
    operator_id: str
    ip: str
    mac: str | None  # normalised, or None when missing or malformed
    site_id: str | None

    @property
    def device_key(self) -> str:
        return f"{_PREFIX}:mac:{self.mac}" if self.mac else f"{_PREFIX}:nomac:{self.ip}"

    @property
    def ip_key(self) -> str:
        return f"{_PREFIX}:ip:{self.ip}"

    @property
    def operator_key(self) -> str:
        return f"{_PREFIX}:op:{self.operator_id}"

    @property
    def operator_ips_key(self) -> str:
        return f"{_PREFIX}:opips:{self.operator_id}"


def normalize_mac(value: str | None) -> str | None:
    """AA:BB:CC:DD:EE:FF from the usual spellings, else None."""
    raw = (value or "").strip().upper()
    if not _MAC_RE.match(raw):
        return None
    digits = re.sub(r"[^0-9A-F]", "", raw)
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def site_or_none(value) -> str | None:
    """The router's site id as a UUID string, else None. routers.site_id is
    NOT NULL today, but the gateway fallback stringifies it ("None" if that
    ever changed), and a bad value here would break the stats row and the
    alert for that failure."""
    try:
        return str(uuid.UUID(str(value))) if value else None
    except ValueError:
        return None


# ── the limits (platform settings, cached briefly) ────────────────────────

_SETTING_KEYS = {
    "device": "portal_login_fail_limit_mac",
    "ip": "portal_login_fail_limit_ip",
    "operator_alert": "portal_login_fail_alert_operator",
}
_limits_cache: tuple[float, Limits] | None = None


def _config_limits() -> Limits:
    s = get_settings()
    return Limits(device=s.portal_login_fail_limit_mac, ip=s.portal_login_fail_limit_ip,
                  operator_alert=s.portal_login_fail_alert_operator)


def _coerce(value, default: int) -> int:
    from src.modules.platform.settings_service import PORTAL_LOGIN_LIMIT_MAX

    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if 1 <= number <= PORTAL_LOGIN_LIMIT_MAX else default


async def current_limits() -> Limits:
    """The limits from platform_settings, re-read at most once a minute. Own
    session; any failure falls back to config rather than failing the login."""
    global _limits_cache
    now = time.monotonic()
    if _limits_cache is not None and now < _limits_cache[0]:
        return _limits_cache[1]
    defaults = _config_limits()
    try:
        from src.modules.platform.settings_service import get_setting

        async with async_session_factory() as db:
            stored = {field: await get_setting(db, key) for field, key in _SETTING_KEYS.items()}
        limits = Limits(**{field: _coerce(stored[field], getattr(defaults, field)) for field in _SETTING_KEYS})
    except Exception as exc:
        log.error("portal_login_limits_settings_failed", error_type=type(exc).__name__, error=str(exc)[:200])
        limits = defaults
    _limits_cache = (now + LIMITS_CACHE_SECONDS, limits)
    return limits


def reset_limits_cache() -> None:
    global _limits_cache
    _limits_cache = None


# ── check before the lookup, count after a miss ───────────────────────────

def _minutes(ttl_seconds) -> str:
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        ttl = -1
    minutes = max(1, math.ceil((ttl if ttl > 0 else WINDOW_SECONDS) / 60))
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


async def check(attempt: Attempt, limits: Limits) -> None:
    """429 when the device or the IP is at its limit. Fails open."""
    redis = None
    try:
        redis = get_rate_limit_redis()
        device, ip = await redis.mget(attempt.device_key, attempt.ip_key)
        blocked = None
        if int(device or 0) >= limits.device:
            blocked = (attempt.device_key, DEVICE_LIMIT_DETAIL)
        elif int(ip or 0) >= limits.ip:
            blocked = (attempt.ip_key, SITE_LIMIT_DETAIL)
        if blocked is None:
            return
        key, detail = blocked
        try:
            ttl = await redis.ttl(key)
        except Exception:
            ttl = None
    except Exception as exc:
        _fail_open(exc, attempt, stage="check")
        return
    log.info("portal_login_limited", operator_id=attempt.operator_id, client_ip=attempt.ip,
             limit="device" if key == attempt.device_key else "ip", has_mac=attempt.mac is not None)
    raise HTTPException(status_code=429, detail=detail.format(minutes=_minutes(ttl)))


async def record_failure(attempt: Attempt, limits: Limits) -> None:
    """Count one wrong code; record the day's peaks; schedule any alert.
    Never raises: the customer's 400 must not depend on any of it."""
    counts = None
    try:
        redis = get_rate_limit_redis()
        async with redis.pipeline(transaction=True) as pipe:
            for key in (attempt.device_key, attempt.ip_key, attempt.operator_key):
                pipe.incr(key)
                pipe.expire(key, WINDOW_SECONDS, nx=True)
            pipe.sadd(attempt.operator_ips_key, attempt.ip)
            pipe.expire(attempt.operator_ips_key, WINDOW_SECONDS, nx=True)
            pipe.scard(attempt.operator_ips_key)
            results = await pipe.execute()
        counts = {"device": results[0], "ip": results[2], "operator": results[4], "operator_ips": results[8]}
    except Exception as exc:
        _fail_open(exc, attempt, stage="record")

    device_reached = counts is not None and counts["device"] == limits.device
    ip_reached = counts is not None and counts["ip"] == limits.ip
    log.info("portal_login_failed", operator_id=attempt.operator_id, client_ip=attempt.ip,
             has_mac=attempt.mac is not None, **({f"{k}_window": v for k, v in (counts or {}).items()}))
    await _record_stats(attempt, counts, device_reached=device_reached, ip_reached=ip_reached)
    if counts is None:
        return

    kinds = []
    if ip_reached:
        log.warning("portal_login_site_blocked", operator_id=attempt.operator_id, client_ip=attempt.ip,
                    failures=counts["ip"], limit=limits.ip)
        kinds.append(SITE_BLOCKED)
    if counts["operator"] >= limits.operator_alert:
        kinds.append(GUESSING_SUSPECTED)
    for kind in kinds:
        try:
            first = await redis.set(f"{_ALERT_PREFIX}:{kind}:{attempt.operator_id}", "1",
                                    nx=True, ex=ALERT_DEDUPE_SECONDS)
        except Exception as exc:
            _fail_open(exc, attempt, stage="alert_dedupe")
            return
        if first:
            if kind == GUESSING_SUSPECTED:
                log.warning("portal_login_guessing_suspected", operator_id=attempt.operator_id,
                            failures=counts["operator"], connections=counts["operator_ips"],
                            threshold=limits.operator_alert)
            _spawn(_send_alert(kind, attempt, counts, limits))


_STATS_UPSERT = text("""
    INSERT INTO portal_login_failure_stats AS s
        (isp_operator_id, day, client_ip, site_id, failures, peak_ip_window, peak_device_window,
         peak_operator_window, device_limit_reached, ip_limit_reached)
    VALUES (CAST(:op AS uuid), (now() AT TIME ZONE 'UTC')::date, CAST(:ip AS inet), CAST(:site AS uuid), 1,
            :ip_n, :device_n, :op_n, :device_r, :ip_r)
    ON CONFLICT (isp_operator_id, day, client_ip) DO UPDATE SET
        site_id = COALESCE(EXCLUDED.site_id, s.site_id),
        failures = s.failures + 1,
        peak_ip_window = GREATEST(s.peak_ip_window, EXCLUDED.peak_ip_window),
        peak_device_window = GREATEST(s.peak_device_window, EXCLUDED.peak_device_window),
        peak_operator_window = GREATEST(s.peak_operator_window, EXCLUDED.peak_operator_window),
        device_limit_reached = s.device_limit_reached + EXCLUDED.device_limit_reached,
        ip_limit_reached = s.ip_limit_reached + EXCLUDED.ip_limit_reached,
        last_failure_at = now()
""")


async def _record_stats(attempt: Attempt, counts: dict | None, *, device_reached: bool, ip_reached: bool) -> None:
    """One upsert per wrong code. With Redis down the failure is still
    counted; the window peaks are unknown and left as they were."""
    c = counts or {}
    try:
        async with async_session_factory() as db:
            await db.execute(_STATS_UPSERT, {
                "op": attempt.operator_id, "ip": attempt.ip, "site": attempt.site_id or None,
                "ip_n": c.get("ip", 0), "device_n": c.get("device", 0), "op_n": c.get("operator", 0),
                "device_r": int(device_reached), "ip_r": int(ip_reached),
            })
            await db.commit()
    except Exception as exc:
        log.error("portal_login_stats_failed", operator_id=attempt.operator_id, client_ip=attempt.ip,
                  error_type=type(exc).__name__, error=str(exc)[:200])


# ── Redis unavailable: fail open, log, alert ──────────────────────────────

_fail_open_last_logged: float | None = None
_fail_open_suppressed = 0
_unavailable_alerted_at: float | None = None


def _fail_open(exc: Exception, attempt: Attempt, *, stage: str) -> None:
    """Log (at most once a minute, with a count of the rest) and alert the
    platform owner (at most once an hour). The dedupe is per process: Redis,
    where it would otherwise live, is what's down."""
    global _fail_open_last_logged, _fail_open_suppressed, _unavailable_alerted_at
    try:
        now = time.monotonic()
        if _fail_open_last_logged is not None and now - _fail_open_last_logged < FAIL_OPEN_LOG_INTERVAL_SECONDS:
            _fail_open_suppressed += 1
        else:
            _fail_open_last_logged = now
            log.error("portal_login_limits_fail_open", stage=stage, operator_id=attempt.operator_id,
                      client_ip=attempt.ip, error_type=type(exc).__name__, error=str(exc)[:200],
                      suppressed_since_last_log=_fail_open_suppressed)
            _fail_open_suppressed = 0
        if _unavailable_alerted_at is None or now - _unavailable_alerted_at >= ALERT_DEDUPE_SECONDS:
            _unavailable_alerted_at = now
            _spawn(_send_unavailable_alert(type(exc).__name__, stage))
    except Exception:
        pass  # a fault here must not break the login either


def reset_fail_open_state() -> None:
    global _fail_open_last_logged, _fail_open_suppressed, _unavailable_alerted_at
    _fail_open_last_logged, _fail_open_suppressed, _unavailable_alerted_at = None, 0, None


# ── alert delivery (after the response; never raises) ─────────────────────

_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    """Run an alert without holding up the customer's response. The set keeps
    a reference so the task isn't collected before it finishes."""
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def wait_for_alerts() -> None:
    """For tests: let scheduled alerts finish."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def _operator_context(attempt: Attempt) -> tuple[str, str | None, str | None]:
    """(operator name, contact email, site name) for the message."""
    from src.db.models import ISPOperator, Site

    async with async_session_factory() as db:
        name, email = (await db.execute(
            select(ISPOperator.name, ISPOperator.contact_email).where(ISPOperator.id == uuid.UUID(attempt.operator_id))
        )).one()
        site = None
        if attempt.site_id:
            site = (await db.execute(select(Site.name).where(Site.id == uuid.UUID(attempt.site_id)))).scalar_one_or_none()
    return name, email, site


async def _platform_owner_emails() -> list[str]:
    from src.db.models import PlatformOwner

    async with async_session_factory() as db:
        return list((await db.execute(
            select(PlatformOwner.email).where(PlatformOwner.is_active == True)  # noqa: E712
        )).scalars())


async def _email(to: str | None, subject: str, paragraphs: list[str]) -> None:
    if not to:
        return
    from src.modules.notifications.email.service import get_email_service
    from src.modules.notifications.template_catalog import wrap_html

    body_text = "\n\n".join(paragraphs)
    body_html = wrap_html("".join(f"<p>{html.escape(p)}</p>" for p in paragraphs))
    try:
        await get_email_service().send(to=to, subject=subject, body_html=body_html, body_text=body_text)
    except Exception as exc:
        log.error("portal_login_alert_email_failed", to=to, subject=subject, error=str(exc)[:200])


def _connections(n: int) -> str:
    return "1 connection" if n == 1 else f"{n} connections"


async def _send_alert(kind: str, attempt: Attempt, counts: dict, limits: Limits) -> None:
    from src.modules import audit
    from src.modules.platform.settings_service import get_platform_name

    try:
        isp_name, contact_email, site_name = await _operator_context(attempt)
        platform = await get_platform_name()
        where = f"{attempt.ip}" + (f", site {site_name}" if site_name else "")
        if kind == SITE_BLOCKED:
            summary = f"Code logins paused at one hotspot connection ({where}): {counts['ip']} wrong codes in 15 minutes"
            subject = f"{platform}: code logins paused at a hotspot — {isp_name}"
            paragraphs = [
                f"{counts['ip']} wrong voucher codes were typed from one hotspot connection ({where}) within "
                f"15 minutes, so code logins from it are paused until those 15 minutes are up. Customers there "
                f"can't log in with a code until then, even one they have just bought.",
                "If customers were mistyping codes, nothing needs doing. If you didn't expect this, someone "
                "there may be trying codes at random.",
                "You'll get at most one of these an hour.",
            ]
        else:
            summary = (f"Possible code guessing: {counts['operator']} wrong codes in 15 minutes from "
                       f"{_connections(counts['operator_ips'])}")
            subject = f"{platform}: possible voucher code guessing — {isp_name}"
            paragraphs = [
                f"{counts['operator']} wrong voucher codes were typed at {isp_name}'s hotspots in the last 15 "
                f"minutes, from {_connections(counts['operator_ips'])}. The alert is raised at "
                f"{limits.operator_alert}; it usually means someone is trying codes at random.",
                f"Each connection is still held to {limits.ip} wrong codes per 15 minutes, and codes keep "
                f"working for customers who have them.",
                "You'll get at most one of these an hour.",
            ]
        await audit.record_detached(
            AUDIT_ACTIONS[kind], actor=audit.Actor.system(), request_info=audit.RequestInfo(),
            isp_operator_id=uuid.UUID(attempt.operator_id), target_type="isp_operator", target_id=attempt.operator_id,
            summary=summary,
            detail={"client_ip": attempt.ip, "site_id": attempt.site_id, "site_name": site_name,
                    "window_minutes": WINDOW_SECONDS // 60, "failures_from_ip": counts["ip"],
                    "failures_for_operator": counts["operator"], "connections": counts["operator_ips"],
                    "limit_ip": limits.ip, "alert_operator": limits.operator_alert},
        )
        await _email(contact_email, subject, paragraphs)
        if kind == GUESSING_SUSPECTED:
            for owner_email in await _platform_owner_emails():
                await _email(owner_email, f"{platform}: possible voucher code guessing at {isp_name}",
                             [f"Operator: {isp_name} ({attempt.operator_id}).", *paragraphs[:2]])
    except Exception as exc:
        log.error("portal_login_alert_failed", kind=kind, operator_id=attempt.operator_id, error=str(exc)[:200])


async def _send_unavailable_alert(error_type: str, stage: str) -> None:
    from src.modules import audit
    from src.modules.platform.settings_service import get_platform_name

    try:
        await audit.record_detached(
            UNAVAILABLE_ACTION, actor=audit.Actor.system(), request_info=audit.RequestInfo(), isp_operator_id=None,
            summary="Captive portal wrong-code limits are off: Redis can't be reached (failing open)",
            detail={"error_type": error_type, "stage": stage},
        )
        platform = await get_platform_name()
        for owner_email in await _platform_owner_emails():
            await _email(owner_email, f"{platform}: captive portal wrong-code limits are off", [
                f"The captive portal couldn't reach Redis ({error_type}), so voucher code logins are running "
                f"without their wrong-code limits. Codes still log in normally.",
                "Check the Redis container. You'll get at most one of these an hour while it lasts.",
            ])
    except Exception as exc:
        log.error("portal_login_unavailable_alert_failed", error=str(exc)[:200])
