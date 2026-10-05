"""The portal's payment-status poll and the provider webhook confirming the
same payment at the same moment must text the buyer exactly once.

Needs a real Postgres (the guarantee rests on SELECT … FOR UPDATE) and a real
Redis (arq job-id dedup), both throwaway:

  VOUCHER_SMS_RACE_DATABASE_URL  — a database whose name contains "throwaway",
                                   and equal to DATABASE_URL (the app engine
                                   binds DATABASE_URL at import)
  VOUCHER_SMS_RACE_REDIS_URL     — equal to REDIS_URL, and not port 6379
                                   (production Redis on this host)

Skipped otherwise. Writes rows into that database with no teardown.
"""
import asyncio
import os
import uuid

import pytest

RACE_DB = os.getenv("VOUCHER_SMS_RACE_DATABASE_URL", "")
RACE_REDIS = os.getenv("VOUCHER_SMS_RACE_REDIS_URL", "")
ROUNDS = 25

pytestmark = [
    pytest.mark.skipif(
        not RACE_DB
        or "throwaway" not in RACE_DB.rsplit("/", 1)[-1]
        or os.getenv("DATABASE_URL") != RACE_DB
        or not RACE_REDIS
        or os.getenv("REDIS_URL") != RACE_REDIS
        or RACE_REDIS.rstrip("/").endswith(":6379"),
        reason="needs VOUCHER_SMS_RACE_DATABASE_URL / VOUCHER_SMS_RACE_REDIS_URL (throwaway, == DATABASE_URL / REDIS_URL)",
    ),
    pytest.mark.real_voucher_sms_enqueue,
    pytest.mark.asyncio(loop_scope="module"),
]

if RACE_DB and RACE_REDIS:  # imports that bind the engine only happen when the guard can pass
    from arq import func
    from arq.worker import Worker
    from sqlalchemy import text

    from src.db.base import async_session_factory
    from src.jobs import voucher_sms
    from src.jobs.queue import get_redis_pool
    from src.modules.payments.service import PaymentService
    from src.modules.sms import voucher_delivery
    from src.modules.sms.types import SMSSendResult


async def sql(query, **params):
    async with async_session_factory() as db:
        result = await db.execute(text(query), params)
        await db.commit()
        return result


async def make_operator():
    ids = {k: uuid.uuid4() for k in ("op", "town", "site", "plan")}
    slug = f"race-{ids['op'].hex[:10]}"
    await sql(
        "insert into isp_operators (id,name,slug,contact_email,status) values (:op,:n,:n,:e,'approved')",
        op=ids["op"], n=slug, e=f"{slug}@throwaway.test",
    )
    await sql("insert into towns (id,name,region,isp_operator_id) values (:t,'T','Volta',:op)", t=ids["town"], op=ids["op"])
    await sql(
        "insert into sites (id,town_id,name,address,isp_operator_id) values (:s,:t,'S','addr',:op)",
        s=ids["site"], t=ids["town"], op=ids["op"],
    )
    await sql(
        "insert into plans (id,name,type,duration_minutes,download_speed_kbps,upload_speed_kbps,price_ghs,isp_operator_id)"
        " values (:p,'Daily','time',1440,1024,512,5,:op)",
        p=ids["plan"], op=ids["op"],
    )
    return ids


async def make_pending_tx(ids, phone):
    tx_id, ref = uuid.uuid4(), uuid.uuid4().hex  # unique first 8 chars -> unique PAY- batch_id
    await sql(
        "insert into payment_transactions (id,plan_id,site_id,amount_ghs,payment_method,provider,"
        "internal_reference,status,isp_operator_id,phone_number)"
        " values (:id,:p,:s,5,'mtn_momo','paystack',:ref,'pending',:op,:ph)",
        id=tx_id, p=ids["plan"], s=ids["site"], ref=ref, op=ids["op"], ph=phone,
    )
    return tx_id, ref


async def confirm(ref, trigger_source):
    async with async_session_factory() as db:
        return await PaymentService().resolve_successful_payment(
            db, internal_reference=ref, trigger_source=trigger_source
        )


async def test_poll_and_webhook_racing_send_exactly_one_sms(monkeypatch):
    sent = []

    class _Sender:
        async def send(self, *, to, message):
            sent.append(to)
            return SMSSendResult(success=True, provider_reference=f"m-{uuid.uuid4().hex}")

    async def _resolve(_db, _op):
        return ("hubtel", {"client_id": "a", "client_secret": "b", "from": "SID"})

    monkeypatch.setattr(voucher_delivery, "resolve_active_sms_provider", _resolve)
    monkeypatch.setattr(voucher_delivery, "build_sms_provider", lambda k, c: _Sender())

    redis = await get_redis_pool()
    await redis.flushdb()  # throwaway Redis (guarded above)

    ids = await make_operator()
    txs = []
    for i in range(ROUNDS):
        phone = f"23320{i:07d}"
        tx_id, ref = await make_pending_tx(ids, phone)
        # Both confirmations in flight at once, each on its own session —
        # exactly what a portal poll and a webhook job do.
        results = await asyncio.gather(confirm(ref, "portal_poll"), confirm(ref, "webhook"))
        assert {r.status for r in results} == {"success"}
        # A late duplicate enqueue (e.g. a retried webhook) is refused by job id.
        await voucher_sms.enqueue_voucher_sms(tx_id)
        txs.append((tx_id, phone))

    queued = await redis.zcard("arq:queue")
    assert queued == ROUNDS, f"expected one queued job per payment, found {queued}"

    worker = Worker(
        functions=[func(voucher_sms.send_voucher_sms, max_tries=1)],
        redis_pool=redis, burst=True, poll_delay=0.05, handle_signals=False,
    )
    try:
        await worker.main()
    finally:
        await worker.close()

    for tx_id, phone in txs:
        vouchers = (await sql(
            "select count(*) from vouchers where id = (select voucher_id from payment_transactions where id=:id)"
            " or batch_id = (select 'PAY-' || left(internal_reference, 8) from payment_transactions where id=:id)",
            id=tx_id,
        )).scalar()
        assert vouchers == 1, f"{tx_id}: {vouchers} vouchers"
        assert sent.count(phone) == 1, f"{phone}: {sent.count(phone)} SMS"
    assert len(sent) == ROUNDS
    assert worker.jobs_complete == ROUNDS and worker.jobs_failed == 0
