"""Per-message SMS status (sms_messages, migration 054): tracked_send, the
Arkesel callback_url + status lookup, refresh_status, the callback route and
the jobs. Pure unit tests: database, Redis and Arkesel are faked.
"""
import hashlib
import json
import logging
import types
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.app import app
from src.db.base import get_db
from src.jobs import sms_status
from src.jobs.worker import WorkerSettings
from src.modules.sms import status_tracking
from src.modules.sms.providers.arkesel import ArkeselSMSProvider, MessageStatus
from src.modules.sms.providers.base import key_fingerprint
from src.modules.sms.types import SMSSendResult

SMS_ID = "fd6e819c-b435-49af-94be-d0f81225492b"


def _ark(handler, key="key-123"):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://sms.arkesel.com")
    return ArkeselSMSProvider(api_key=key, sender_id="ipadmin", client=client)


# ── Arkesel provider additions ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("callback", [None, "https://ip-admin.example.test/api/v1/webhooks/arkesel/sms-status/tok"])
async def test_send_passes_callback_url_only_when_given(callback):
    seen = {}

    async def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "success", "data": [{"id": SMS_ID}]})

    await _ark(handler).send("233201234514", "hi", callback_url=callback)
    assert seen["body"].get("callback_url") == callback
    assert ("callback_url" in seen["body"]) is bool(callback)


@pytest.mark.asyncio
async def test_status_lookup_parses_the_live_shape():
    async def handler(request):
        assert request.method == "GET" and request.url.path == f"/api/v2/sms/{SMS_ID}"
        assert request.headers["api-key"] == "key-123"
        # Recorded from the live API on 2026-10-05 (recipient masked).
        return httpx.Response(200, json={"data": {
            "ID": SMS_ID, "status": "DELIVERED", "sender": "ipadmin", "recipient": "23320****514",
            "message": "…", "message_count": 1, "sent_at_time": "2026-10-05 15:00:34"}, "status": "success"})

    st = await _ark(handler).get_message_status(SMS_ID)
    assert st == MessageStatus(status="DELIVERED", sent_at=datetime(2026, 10, 5, 15, 0, 34, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_status_lookup_tolerates_a_missing_or_odd_sent_time():
    async def handler(request):
        return httpx.Response(200, json={"data": {"status": "submitted", "sent_at_time": "soon"}, "status": "success"})

    assert await _ark(handler).get_message_status(SMS_ID) == MessageStatus(status="SUBMITTED", sent_at=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(401, json={"message": "Invalid key", "status": "error"}),
    httpx.Response(200, json={"status": "error", "message": "not found"}),
    httpx.Response(500, text="boom"),
])
async def test_status_lookup_failures_raise(response):
    async def handler(request):
        return response

    with pytest.raises(ValueError):
        await _ark(handler).get_message_status(SMS_ID)


# ── tracked_send ─────────────────────────────────────────────────────────


@pytest.fixture
def recorded(monkeypatch):
    rows = []

    async def _record(**kw):
        rows.append(kw)
        return True

    monkeypatch.setattr(status_tracking, "record_accepted", _record)
    return rows


def _base(monkeypatch, base):
    async def _webhook_base_url():
        return base

    monkeypatch.setattr("src.modules.webhooks.urls.webhook_base_url", _webhook_base_url)


@pytest.mark.asyncio
async def test_arkesel_send_on_https_gets_a_per_message_callback(monkeypatch, recorded):
    _base(monkeypatch, "https://ip-admin.example.test")
    seen = {}

    async def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "success", "data": [{"id": SMS_ID}]})

    op = uuid.uuid4()
    result = await status_tracking.tracked_send(
        _ark(handler), to="233201234514", message="hi", purpose="voucher_purchase",
        provider_key="arkesel_platform", credential_scope="platform_gateway", operator_id=op)
    assert result.success
    url = seen["body"]["callback_url"]
    prefix = "https://ip-admin.example.test/api/v1/webhooks/arkesel/sms-status/"
    assert url.startswith(prefix)
    token = url[len(prefix):]
    assert len(token) >= 32
    r = recorded[0]
    assert r["callback_token"] == token
    assert status_tracking.token_matches(token, status_tracking.token_hash(r["callback_token"]))
    assert r["credential_fp"] == key_fingerprint("key-123") and r["sender_id"] == "ipadmin"
    assert r["provider_reference"] == SMS_ID and r["operator_id"] == op


@pytest.mark.asyncio
async def test_no_callback_when_the_public_base_is_not_https(monkeypatch, recorded):
    _base(monkeypatch, "http://34.122.11.114")
    seen = {}

    async def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "success", "data": [{"id": SMS_ID}]})

    await status_tracking.tracked_send(
        _ark(handler), to="233201234514", message="hi", purpose="otp_reset",
        provider_key="arkesel", credential_scope="platform_notification")
    assert "callback_url" not in seen["body"]
    assert recorded[0]["callback_token"] is None  # still recorded: the poll covers it


@pytest.mark.asyncio
async def test_a_refused_send_is_not_recorded(monkeypatch, recorded):
    _base(monkeypatch, "https://ip-admin.example.test")

    async def handler(request):
        return httpx.Response(200, json={"status": "error", "message": "Invalid sender"})

    result = await status_tracking.tracked_send(
        _ark(handler), to="233201234514", message="hi", purpose="notification",
        provider_key="arkesel", credential_scope="platform_notification")
    assert not result.success and recorded == []


@pytest.mark.asyncio
async def test_record_failure_never_raises(monkeypatch, caplog):
    class _Broken:
        async def __aenter__(self):
            raise ConnectionError("db down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(status_tracking, "async_session_factory", lambda: _Broken())
    ok = await status_tracking.record_accepted(
        provider_key="arkesel", credential_scope="operator", credential_fp="x" * 12, sender_id="S", to="233201234514",
        purpose="voucher_purchase", provider_reference=SMS_ID, callback_token=None, operator_id=None,
        payment_transaction_id=None)
    assert ok is False
    assert "sms_message_record_failed" in caplog.text


# ── refresh_status ───────────────────────────────────────────────────────


class _Db:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _row(**kw):
    base = dict(id=uuid.uuid4(), provider="arkesel_platform", provider_reference=SMS_ID,
                credential_scope="platform_gateway", credential_fp=key_fingerprint("key-123"),
                accepted_at=datetime(2026, 10, 5, 14, 6, 22, tzinfo=timezone.utc), provider_status=None,
                provider_sent_at=None, final_status_at=None, status_source=None, last_checked_at=None,
                check_count=0, purpose="voucher_purchase", recipient_masked="23320****514")
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def arkesel_says(monkeypatch):
    state = {"status": None, "calls": 0, "key": "key-123"}

    async def _creds(db, row):
        return {"api_key": state["key"], "sender_id": "ipadmin"}

    async def _lookup(self, message_id):
        state["calls"] += 1
        if isinstance(state["status"], Exception):
            raise state["status"]
        return state["status"]

    monkeypatch.setattr(status_tracking, "_status_credentials", _creds)
    monkeypatch.setattr(ArkeselSMSProvider, "get_message_status", _lookup)
    return state


@pytest.mark.asyncio
async def test_final_status_is_stored_with_its_source(arkesel_says):
    sent = datetime(2026, 10, 5, 14, 6, 25, tzinfo=timezone.utc)
    arkesel_says["status"] = MessageStatus("DELIVERED", sent)
    row, db = _row(), _Db()
    assert await status_tracking.refresh_status(db, row, source="callback") == "final"
    assert row.provider_status == "DELIVERED" and row.provider_sent_at == sent
    assert row.final_status_at is not None and row.status_source == "callback"
    assert row.check_count == 1 and row.last_checked_at is not None and db.commits == 1


@pytest.mark.asyncio
async def test_non_final_status_keeps_polling(arkesel_says):
    arkesel_says["status"] = MessageStatus("SUBMITTED", None)
    row = _row()
    assert await status_tracking.refresh_status(_Db(), row, source="poll") == "pending"
    assert row.final_status_at is None and row.provider_status == "SUBMITTED"


@pytest.mark.asyncio
async def test_a_later_read_does_not_move_final_status_at(arkesel_says):
    first = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
    arkesel_says["status"] = MessageStatus("DELIVERED", None)
    row = _row(final_status_at=first, status_source="callback", provider_status="DELIVERED")
    await status_tracking.refresh_status(_Db(), row, source="poll")
    assert row.final_status_at == first and row.status_source == "callback"


@pytest.mark.asyncio
async def test_rotated_key_skips_the_lookup(arkesel_says, caplog):
    arkesel_says["key"] = "a-new-key"
    row = _row()
    assert await status_tracking.refresh_status(_Db(), row, source="poll") == "credential_changed"
    assert arkesel_says["calls"] == 0 and row.check_count == 1
    assert "sms_status_credential_changed" in caplog.text and "a-new-key" not in caplog.text


@pytest.mark.asyncio
async def test_lookup_failure_is_logged_not_raised(arkesel_says, caplog):
    arkesel_says["status"] = ValueError("Arkesel status lookup failed: HTTP 500")
    row = _row()
    assert await status_tracking.refresh_status(_Db(), row, source="poll") == "lookup_failed"
    assert row.check_count == 1 and row.final_status_at is None


@pytest.mark.asyncio
async def test_dispatch_delay_over_five_minutes_is_warned_once(arkesel_says, caplog):
    caplog.set_level(logging.INFO, logger="sms.status_tracking")
    # The 2026-10-05 purchase: accepted 14:06:22, sent to the network 15:00:34.
    arkesel_says["status"] = MessageStatus("SUBMITTED", datetime(2026, 10, 5, 15, 0, 34, tzinfo=timezone.utc))
    row = _row()
    await status_tracking.refresh_status(_Db(), row, source="poll")
    warned = [r for r in caplog.records if "sms_dispatch_delayed" in r.getMessage()]
    assert len(warned) == 1 and "delay_s=3252" in warned[0].getMessage()
    caplog.clear()
    arkesel_says["status"] = MessageStatus("DELIVERED", datetime(2026, 10, 5, 15, 0, 34, tzinfo=timezone.utc))
    await status_tracking.refresh_status(_Db(), row, source="poll")
    assert not [r for r in caplog.records if "sms_dispatch_delayed" in r.getMessage()]


@pytest.mark.asyncio
async def test_untracked_providers_are_not_looked_up(arkesel_says):
    assert await status_tracking.refresh_status(_Db(), _row(provider="hubtel"), source="poll") == "untracked"
    assert arkesel_says["calls"] == 0


# ── The callback route ───────────────────────────────────────────────────


TOKEN = "t" * 32


class _RouteDb:
    def __init__(self, row):
        self.row = row
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        row = self.row

        class _R:
            def scalar_one_or_none(self):
                return row

        return _R()


@pytest.fixture
def callback(monkeypatch):
    state = {"row": types.SimpleNamespace(id=uuid.uuid4(), callback_token_hash=status_tracking.token_hash(TOKEN)),
             "enqueued": []}

    async def _db():
        yield _RouteDb(state["row"])

    async def _enqueue(message_id, reported):
        state["enqueued"].append((message_id, reported))
        return True

    async def _no_limit(*a, **k):
        return None

    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(sms_status, "enqueue_status_refresh", _enqueue)
    monkeypatch.setattr("src.modules.webhooks.routes.enforce_rate_limit", _no_limit)
    yield state
    app.dependency_overrides.pop(get_db, None)


async def call(method, path, **kw):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://ip-admin.example.test") as client:
        return await client.request(method, path, **kw)


PATH = f"/api/v1/webhooks/arkesel/sms-status/{TOKEN}"


@pytest.mark.asyncio
async def test_documented_get_callback_enqueues_a_refresh(callback):
    res = await call("GET", PATH, params={"sms_id": SMS_ID, "status": "DELIVERED"})
    assert res.status_code == 200 and res.json() == {"ok": True}
    assert callback["enqueued"] == [(callback["row"].id, "DELIVERED")]


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    {"params": {"sms_id": SMS_ID, "status": "DELIVERED"}},
    {"data": {"sms_id": SMS_ID, "status": "DELIVERED"}},
    {"json": {"sms_id": SMS_ID, "status": "DELIVERED"}},
])
async def test_post_callback_in_any_shape_is_accepted(callback, kw):
    res = await call("POST", PATH, **kw)
    assert res.status_code == 200 and len(callback["enqueued"]) == 1


@pytest.mark.asyncio
async def test_wrong_token_is_404_and_triggers_nothing(callback):
    res = await call("GET", f"/api/v1/webhooks/arkesel/sms-status/{'x' * 32}", params={"sms_id": SMS_ID, "status": "DELIVERED"})
    assert res.status_code == 404 and callback["enqueued"] == []


@pytest.mark.asyncio
async def test_unknown_message_is_404(callback):
    callback["row"] = None
    res = await call("GET", PATH, params={"sms_id": SMS_ID, "status": "DELIVERED"})
    assert res.status_code == 404 and callback["enqueued"] == []


@pytest.mark.asyncio
async def test_missing_sms_id_is_404_without_a_lookup(callback):
    res = await call("GET", PATH, params={"status": "DELIVERED"})
    assert res.status_code == 404 and callback["enqueued"] == []


@pytest.mark.asyncio
async def test_the_raw_callback_is_logged_with_digits_masked(callback, caplog):
    caplog.set_level(logging.INFO, logger="webhooks.routes")
    await call("POST", PATH, params={"sms_id": SMS_ID, "status": "DELIVERED"}, data={"recipient": "233201234514"})
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("arkesel_sms_callback "))
    assert "method=POST" in line and f"sms_id={SMS_ID}" in line and "status=DELIVERED" in line
    assert "recipient=23320****514" in line and "233201234514" not in line


@pytest.mark.asyncio
async def test_the_callback_route_needs_no_auth_and_gets_the_api_csp(callback):
    res = await call("GET", PATH, params={"sms_id": SMS_ID, "status": "DELIVERED"})
    assert res.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"


# ── Jobs ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.real_voucher_sms_enqueue
async def test_refresh_job_id_is_per_message_and_reported_status(monkeypatch):
    seen = []

    class _Pool:
        async def enqueue_job(self, name, *args, _job_id=None):
            seen.append((name, args, _job_id))

        async def aclose(self):
            pass

    async def _pool():
        return _Pool()

    monkeypatch.setattr(sms_status, "get_redis_pool", _pool)
    mid = uuid.uuid4()
    await sms_status.enqueue_status_refresh(mid, "DELIVERED")
    await sms_status.enqueue_status_refresh(mid, "not/a status!")
    assert seen == [("refresh_sms_status", (str(mid),), f"sms_status:{mid}:DELIVERED"),
                    ("refresh_sms_status", (str(mid),), f"sms_status:{mid}:notastatus")]


def test_worker_registers_the_status_jobs():
    names = [getattr(f, "name", None) or f.__name__ for f in WorkerSettings.functions]
    assert "refresh_sms_status" in names and "poll_sms_statuses" in names
    cron = next(c for c in WorkerSettings.cron_jobs if c.name == "cron:poll_sms_statuses")
    assert sorted(cron.minute) == [3, 8, 13, 18, 23, 28, 33, 38, 43, 48, 53, 58]


def test_poll_window_and_interval_are_the_approved_ones():
    assert status_tracking.POLL_INTERVAL == timedelta(minutes=5)
    assert status_tracking.POLL_WINDOW == timedelta(hours=24)
