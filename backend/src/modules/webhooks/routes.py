from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import ISPOperator, OperatorPaymentCredential
from src.jobs.queue import get_redis_pool
from src.middleware.rate_limit import enforce_rate_limit
from src.modules.payments.providers.registry import build_payment_provider
from src.modules.payments.provider_resolver import load_credentials

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


async def _process_webhook(
    provider_key: str,
    operator_slug: str,
    request: Request,
    db: AsyncSession,
) -> dict:
    """Verify and enqueue one operator-scoped payment webhook.

    One explicit route per supported provider delegates here (see below) rather
    than a catch-all ``/{provider}/{operator_slug}`` — a catch-all would also
    match ``/api/v1/webhooks/platform-billing/paystack`` and shadow that handler.

    The Paystack path through here is byte-for-byte what the dedicated Paystack
    route did before: same rate-limit key, same operator/credential lookups
    (including ``is_active``), same signature handling, same enqueue.
    """
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, f"webhook:{provider_key}", limit=60, window_seconds=60)

    raw_body = await request.body()
    operator = (
        await db.execute(
            select(ISPOperator).where(ISPOperator.slug == operator_slug, ISPOperator.status == "approved")
        )
    ).scalar_one_or_none()
    if not operator:
        raise HTTPException(status_code=404, detail="Unknown operator")

    creds = (
        await db.execute(
            select(OperatorPaymentCredential).where(
                OperatorPaymentCredential.isp_operator_id == operator.id,
                OperatorPaymentCredential.provider == provider_key,
                OperatorPaymentCredential.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if not creds:
        raise HTTPException(status_code=404, detail="Payment credentials are not configured")

    # PaystackProvider falls back to secret_key internally when webhook_secret is
    # None, matching the old route's explicit `webhook_secret or secret_key`.
    provider = build_payment_provider(provider_key, load_credentials(creds), callback_url=None)
    try:
        parsed = await provider.handle_webhook(dict(request.headers), raw_body)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    redis = await get_redis_pool()
    await redis.enqueue_job(
        "process_webhook_event",
        provider_key,
        parsed.internal_reference,
        parsed.status.value,
        parsed.provider_reference,
        raw_body.decode("utf-8"),
    )
    return {"ok": True}


@router.post("/paystack/{operator_slug}")
async def receive_paystack_webhook(operator_slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    # Path and semantics unchanged — the two live operators' Paystack dashboards
    # point here and must keep working with zero action.
    return await _process_webhook("paystack", operator_slug, request, db)


@router.post("/flutterwave/{operator_slug}")
async def receive_flutterwave_webhook(operator_slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    return await _process_webhook("flutterwave", operator_slug, request, db)
