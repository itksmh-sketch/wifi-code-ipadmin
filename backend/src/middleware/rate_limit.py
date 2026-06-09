from fastapi import HTTPException
from redis.asyncio import Redis

from src.config import get_settings


_redis_client: Redis | None = None


def get_rate_limit_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _is_local_ip(ip: str) -> bool:
    return ip in {"127.0.0.1", "::1", "localhost", "testclient"} or ip.startswith("172.18.")


async def enforce_rate_limit(ip: str, bucket: str, limit: int = 10, window_seconds: int = 60) -> None:
    if _is_local_ip(ip):
        return
    try:
        redis = get_rate_limit_redis()
        key = f"rl:{bucket}:{ip}"
        current = await redis.incr(key)
        if current == 1:
            await redis.expire(key, window_seconds)
        if current > limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    except HTTPException:
        raise
    except Exception:
        # Redis unavailable — fail open (allow the request through)
        pass
