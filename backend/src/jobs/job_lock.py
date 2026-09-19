"""Non-overlapping guard for arq cron jobs.

arq's ``cron(unique=True)`` builds its job id as ``f'{name}:{next_run}'``, so it
only dedupes the *same* tick across multiple workers. Consecutive ticks get
different ids and, with ``max_jobs`` defaulting to 10, a slow run does not block
the next one — two runs of the same job can be in flight at once.

Why Redis rather than a Postgres advisory lock
----------------------------------------------
Both Postgres options fail here for concrete reasons:

* ``pg_advisory_xact_lock`` (the existing idiom in ``wireguard/service.py``) is
  transaction-scoped, but ``transition_voucher_status`` commits per voucher, so
  the lock would be released on the first voucher and leave the rest of the run
  unguarded.
* ``pg_try_advisory_lock`` (session-scoped) survives commits, but the worker
  connects through PgBouncer in ``pool_mode = transaction``. A session-scoped
  lock binds to a *server* connection that PgBouncer hands to other clients
  between transactions, so the lock can outlive its owner and the unlock can
  land on a different connection entirely.

arq already holds a Redis pool and passes it as ``ctx['redis']``, so ``SET NX
EX`` is the cheapest correct primitive: atomic, TTL-bounded so a crashed worker
self-heals instead of wedging the job forever, and untouched by PgBouncer.
"""
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def job_lock(redis, key: str, ttl_seconds: int):
    """Yield True if this run owns the lock, False if a previous run still holds it.

    Releases only when the stored token is still ours, so a run that overran its
    TTL cannot delete a lock that a later run legitimately acquired — and a run
    that skips never touches the in-flight owner's lock.
    """
    if redis is None:
        # Manual/direct invocation (no arq ctx). Nothing to serialize against.
        logger.warning("job_lock_unavailable_running_unguarded", module=__name__, key=key)
        yield True
        return

    token = uuid4().hex
    acquired = await redis.set(key, token, ex=ttl_seconds, nx=True)
    if not acquired:
        yield False
        return
    try:
        yield True
    finally:
        current = await redis.get(key)
        if current is not None and (current == token or current == token.encode()):
            await redis.delete(key)
        else:
            logger.warning("job_lock_expired_before_release", module=__name__, key=key)
