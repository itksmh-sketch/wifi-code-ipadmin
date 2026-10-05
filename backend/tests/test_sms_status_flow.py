"""SMS delivery status end to end, on a real database and Redis: a tracked
send records the message, Arkesel's callback (documented GET shape) reaches
the real route, the real arq worker reads the status back from a faked
Arkesel API and stores it; the 5-minute poll covers a message with no
callback; a message older than 24 hours is no longer polled.

Throwaway only, like the other flow suites:
  SMS_STATUS_FLOW_DATABASE_URL  name contains "throwaway", == DATABASE_URL
  SMS_STATUS_FLOW_REDIS_URL     == REDIS_URL, not port 6379
Skipped otherwise. Leaves rows behind (no teardown).
"""
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

FLOW_DB = os.getenv("SMS_STATUS_FLOW_DATABASE_URL", "")
FLOW_REDIS = os.getenv("SMS_STATUS_FLOW_REDIS_URL", "")

pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB
        or not FLOW_REDIS or os.getenv("REDIS_URL") != FLOW_REDIS or FLOW_REDIS.rstrip("/").endswith(":6379"),
        reason="needs SMS_STATUS_FLOW_DATABASE_URL / SMS_STATUS_FLOW_REDIS_URL (throwaway)",
    ),
    pytest.mark.real_voucher_sms_enqueue,
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB and FLOW_REDIS:
    import httpx
    from arq.worker import Worker
    from sqlalchemy import text

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import SMSMessage
    from src.jobs import sms_status
    from src.jobs.queue import get_redis_pool
    from src.modules.sms import status_tracking
    from src.modules.sms.providers.arkesel import ArkeselSMSProvider

KEY = "flow-key-123"
SENT_AT = "2026-10-05 15:00:34"


class FakeArkesel:
    """Answers send and status lookups; status per message id is settable."""

    def __init__(self):
        self.status: dict[str, str] = {}
        self.sent_bodies: list[dict] = []
        self.lookups: list[str] = []

    def handler(self, request):
        if request.method == "POST" and request.url.path == "/api/v2/sms/send":
            body = json.loads(request.content)
            self.sent_bodies.append(body)
            mid = str(uuid.uuid4())
            self.status[mid] = "SUBMITTED"
            return httpx.Response(200, json={"status": "success", "data": [{"id": mid, "recipient": body["recipients"][0]}]})
        mid = request.url.path.rsplit("/", 1)[-1]
        self.lookups.append(mid)
        return httpx.Response(200, json={"status": "success", "data": {
            "ID": mid, "status": self.status[mid], "sent_at_time": SENT_AT}})


async def sql(query, **params):
    async with async_session_factory() as db:
        result = await db.execute(text(query), params)
        await db.commit()
        return result


async def row_for(ref):
    async with async_session_factory() as db:
        return (await db.execute(text("select * from sms_messages where provider_reference=:r"), {"r": ref})).mappings().one()


async def test_callback_poll_and_window(monkeypatch):
    fake = FakeArkesel()

    async def _base():
        return "https://staging.example.test"

    async def _creds(db, row):
        return {"api_key": KEY, "sender_id": "ipadmin"}

    class _Provider(ArkeselSMSProvider):  # the refresh builds its own provider: route it to the fake
        def __init__(self, **kw):
            kw.setdefault("client", httpx.AsyncClient(transport=httpx.MockTransport(fake.handler),
                                                      base_url="https://sms.arkesel.com"))
            super().__init__(**kw)

    monkeypatch.setattr("src.modules.webhooks.urls.webhook_base_url", _base)
    monkeypatch.setattr(status_tracking, "_status_credentials", _creds)
    monkeypatch.setattr(status_tracking, "ArkeselSMSProvider", _Provider)

    redis = await get_redis_pool()
    await redis.flushdb()
    op = uuid.uuid4()
    await sql("insert into isp_operators (id,name,slug,contact_email,status) values (:op,:n,:n,:e,'approved')",
              op=op, n=f"sms-flow-{op.hex[:10]}", e=f"sms-flow-{op.hex[:10]}@throwaway.test")

    # 1. Two tracked sends: both recorded, both asked for a per-message callback.
    async def send(purpose):
        result = await status_tracking.tracked_send(
            _Provider(api_key=KEY, sender_id="ipadmin"), to="233201234514", message="hi", purpose=purpose, provider_key="arkesel_platform",
            credential_scope="platform_gateway", operator_id=op)
        assert result.success
        return result.provider_reference, fake.sent_bodies[-1]["callback_url"]

    ref_cb, url_cb = await send("voucher_purchase")
    ref_poll, _ = await send("voucher_purchase")
    r = await row_for(ref_cb)
    assert r["recipient_masked"] == "23320****514" and r["credential_scope"] == "platform_gateway"
    assert r["callback_token_hash"] == status_tracking.token_hash(url_cb.rsplit("/", 1)[-1])
    assert r["provider_status"] is None and r["final_status_at"] is None

    # 2. Arkesel's callback (documented: GET ?sms_id=&status=) on the real route.
    fake.status[ref_cb] = "DELIVERED"
    path = url_cb.replace("https://staging.example.test", "")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://staging.example.test") as client:
        ok = await client.get(path, params={"sms_id": ref_cb, "status": "DELIVERED"})
        dup = await client.get(path, params={"sms_id": ref_cb, "status": "DELIVERED"})
        forged = await client.get(path, params={"sms_id": ref_poll, "status": "DELIVERED"})  # right URL, other message
    assert ok.status_code == 200 and dup.status_code == 200 and forged.status_code == 404
    assert await redis.zcard("arq:queue") == 1, "a repeated callback must not queue a second read"

    worker = Worker(functions=[sms_status.refresh_sms_status], redis_pool=redis, burst=True,
                    poll_delay=0.05, handle_signals=False)
    try:
        await worker.main()
    finally:
        await worker.close()
    r = await row_for(ref_cb)
    assert r["provider_status"] == "DELIVERED" and r["status_source"] == "callback"
    assert r["final_status_at"] is not None
    assert r["provider_sent_at"] == datetime(2026, 10, 5, 15, 0, 34, tzinfo=timezone.utc)

    # 3. No callback for the other message: the poll reads it (still SUBMITTED),
    #    skips it while it was checked < 5 min ago, then reads DELIVERED.
    stats = await sms_status.poll_sms_statuses()
    assert stats.get("pending") == 1 and (await row_for(ref_poll))["provider_status"] == "SUBMITTED"
    fake.status[ref_poll] = "DELIVERED"
    assert (await sms_status.poll_sms_statuses())["checked"] == 0
    await sql("update sms_messages set last_checked_at = now() - interval '6 minutes' where provider_reference=:r", r=ref_poll)
    assert (await sms_status.poll_sms_statuses()).get("final") == 1
    r = await row_for(ref_poll)
    assert r["status_source"] == "poll" and r["check_count"] == 2

    # 4. Past the 24-hour window, nothing is polled any more.
    ref_old, _ = await send("notification")
    await sql("update sms_messages set accepted_at = now() - interval '25 hours' where provider_reference=:r", r=ref_old)
    lookups = len(fake.lookups)
    assert (await sms_status.poll_sms_statuses())["checked"] == 0 and len(fake.lookups) == lookups
    assert (await row_for(ref_old))["final_status_at"] is None
