import time

import structlog
from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConnectionError as RedisConnectionError,
    RedisError,
    TimeoutError as RedisTimeoutError,
)

from src.config import get_settings


log = structlog.get_logger("rate_limit")

_redis_client: Redis | None = None

# Fail-open events are logged at most once per kind per process per interval; the
# rest are counted and reported on the next line, so an outage can't flood the logs.
FAIL_OPEN_LOG_INTERVAL_SECONDS = 60
_fail_open_last_logged: dict[str, float] = {}
_fail_open_suppressed: dict[str, int] = {}


def get_rate_limit_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _is_local_ip(ip: str) -> bool:
    return ip in {"127.0.0.1", "::1", "localhost", "testclient"} or ip.startswith("172.18.")


def _classify_failure(exc: Exception) -> tuple[str, str]:
    """(kind, log level) for an exception that made the limiter fail open."""
    if isinstance(exc, (AuthenticationError, AuthorizationError)):
        return "redis_rejected", "error"  # credentials/ACL wrong: won't recover by itself
    if isinstance(exc, (RedisConnectionError, RedisTimeoutError)):
        return "redis_unavailable", "warning"  # down, restarting, loading, network blip: self-heals
    if isinstance(exc, RedisError):
        return "redis_rejected", "error"  # reachable but refused the command (OOM, version, ...)
    return "unexpected", "error"  # a bug in this limiter itself


def _log_fail_open(exc: Exception, *, redis: Redis | None, bucket: str, ip: str, limit: int, window_seconds: int) -> None:
    kind, level = _classify_failure(exc)
    now = time.monotonic()
    last = _fail_open_last_logged.get(kind)
    if last is not None and now - last < FAIL_OPEN_LOG_INTERVAL_SECONDS:
        _fail_open_suppressed[kind] = _fail_open_suppressed.get(kind, 0) + 1
        return
    _fail_open_last_logged[kind] = now
    target = None
    if redis is not None:
        kwargs = redis.connection_pool.connection_kwargs  # host/port/db only — never the password
        target = f"{kwargs.get('host')}:{kwargs.get('port')}/{kwargs.get('db', 0)}"
    getattr(log, level)(
        "rate_limit_fail_open",
        kind=kind,
        bucket=bucket,
        client_ip=ip,
        limit=limit,
        window_seconds=window_seconds,
        error_type=type(exc).__name__,
        error=str(exc)[:200],
        redis=target,
        suppressed_since_last_log=_fail_open_suppressed.pop(kind, 0),
        exc_info=exc if kind == "unexpected" else False,
    )


async def enforce_rate_limit(ip: str, bucket: str, limit: int = 10, window_seconds: int = 60) -> None:
    if _is_local_ip(ip):
        return
    redis = None
    try:
        redis = get_rate_limit_redis()
        key = f"rl:{bucket}:{ip}"
        # One MULTI/EXEC: Redis applies INCR and EXPIRE together or not at all, so a
        # dropped connection or cancelled request can't leave a counter with no TTL
        # (which locked that IP out of the bucket permanently). EXPIRE NX sets the TTL
        # only when the key has none: later hits don't extend the fixed window, and a
        # TTL-less key left by the old INCR-then-EXPIRE code heals on its next hit.
        # NX needs Redis >= 7.0 (docker-compose pins redis:7-alpine).
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, window_seconds, nx=True)
            current, _ = await pipe.execute()
    except Exception as exc:
        # Fail open on ANY Exception — Redis faults and bugs in this code alike. The
        # limiter guards every login, the admin API, the captive portal and payment
        # webhooks; letting an exception escape would turn a limiter fault into a
        # platform-wide outage. Failing open is acceptable only because it is logged.
        # BaseException (e.g. CancelledError when the client goes away) still propagates.
        try:
            _log_fail_open(exc, redis=redis, bucket=bucket, ip=ip, limit=limit, window_seconds=window_seconds)
        except Exception:
            pass  # a fault in logging must not break the request either
        return
    if current > limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
