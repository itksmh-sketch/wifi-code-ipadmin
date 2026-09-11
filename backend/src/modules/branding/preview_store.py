"""Transient draft-branding store for the settings-page mobile preview.

Not a DB table — an operator's in-progress (unsaved) edits, written here
debounced from the settings form and read back by the preview iframe's poll.
Reuses the same lazy-init Redis client pattern as middleware/rate_limit.py
rather than a new connection scheme. Self-expiring (TTL, refreshed on every
write) — abandoning an edit mid-session leaves nothing to clean up. Fails
open: if Redis is unreachable, reads return None (preview falls back to the
operator's real persisted branding) and writes are silently dropped, so a
broken preview can never affect the real portal pages.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from redis.asyncio import Redis

from src.config import get_settings

logger = logging.getLogger(__name__)

_TTL_SECONDS = 600  # 10 minutes, refreshed on every write

_redis_client: Redis | None = None


def _get_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _key(operator_id: str) -> str:
    return f"portal:preview_draft:{operator_id}"


async def set_preview_draft(operator_id: str, draft: dict[str, Any]) -> None:
    try:
        redis = _get_redis()
        await redis.setex(_key(operator_id), _TTL_SECONDS, json.dumps(draft))
    except Exception:
        logger.warning("preview_draft_write_failed operator_id=%s", operator_id, exc_info=True)


async def get_preview_draft(operator_id: str) -> dict[str, Any] | None:
    try:
        redis = _get_redis()
        raw = await redis.get(_key(operator_id))
    except Exception:
        logger.warning("preview_draft_read_failed operator_id=%s", operator_id, exc_info=True)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
