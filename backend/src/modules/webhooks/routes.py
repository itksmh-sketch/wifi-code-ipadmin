from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import ISPOperator, OperatorPaymentCredential
from src.jobs.queue import get_redis_pool
from src.middleware.rate_limit import enforce_rate_limit
from src.modules.payments.providers.paystack import PaystackProvider
from src.utils.encryption import decrypt_secret

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


@router.post("/paystack/{operator_slug}")
async def receive_paystack_webhook(operator_slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "webhook:paystack", limit=60, window_seconds=60)

    raw_body = await request.body()
    operator = (
        await db.execute(select(ISPOperator).where(ISPOperator.slug == operator_slug, ISPOperator.status == "approved"))
    ).scalar_one_or_none()
    if not operator:
        raise HTTPException(status_code=404, detail="Unknown operator")
    creds = (
        await db.execute(
            select(OperatorPaymentCredential).where(
                OperatorPaymentCredential.isp_operator_id == operator.id,
                OperatorPaymentCredential.provider == "paystack",
                OperatorPaymentCredential.is_active == True,
            )
        )
    ).scalar_one_or_none()
    if not creds:
        raise HTTPException(status_code=404, detail="Payment credentials are not configured")

    provider = PaystackProvider(
        secret_key=decrypt_secret(creds.secret_key_encrypted),
        public_key=decrypt_secret(creds.public_key_encrypted),
        webhook_secret=decrypt_secret(creds.webhook_secret_encrypted) if creds.webhook_secret_encrypted else decrypt_secret(creds.secret_key_encrypted),
    )
    try:
        parsed = await provider.handle_webhook(dict(request.headers), raw_body)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    redis = await get_redis_pool()
    await redis.enqueue_job(
        "process_webhook_event",
        "paystack",
        parsed.internal_reference,
        parsed.status.value,
        parsed.provider_reference,
        raw_body.decode("utf-8"),
    )
    return {"ok": True}
