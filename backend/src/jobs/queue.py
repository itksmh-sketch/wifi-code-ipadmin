from arq.connections import ArqRedis, RedisSettings, create_pool

from src.config import get_settings


async def get_redis_pool() -> ArqRedis:
    settings = get_settings()
    redis_url = settings.redis_url
    if redis_url.startswith("redis://"):
        host_port = redis_url.replace("redis://", "", 1)
        host, port = host_port.split(":")
        redis_settings = RedisSettings(host=host, port=int(port))
    else:
        redis_settings = RedisSettings()
    return await create_pool(redis_settings)
