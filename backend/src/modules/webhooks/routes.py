import json
import logging
import re
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import ISPOperator, OperatorPaymentCredential, SMSMessage
from src.jobs.queue import get_redis_pool
from src.middleware.rate_limit import enforce_rate_limit
from src.modules.payments.providers.registry import build_payment_provider
from src.modules.payments.provider_resolver import load_credentials

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])
logger = logging.getLogger("webhooks.routes")


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

    The Paystack path through here keeps what the dedicated Paystack route did
    before: same rate-limit key, same credential lookup (including
    ``is_active``), same signature handling, same enqueue.

    The operator lookup deliberately does NOT filter on ``status``. A webhook
    only ever arrives for a transaction the operator's own portal already
    accepted and handed to the provider — the money has moved. Refusing to
    complete it because the operator was suspended in the meantime punishes the
    customer who paid, not the operator: the provider retries into a 404, gives
    up, and the buyer never gets their voucher. Suspension is enforced where a
    purchase *starts* (``/portal/initiate-payment``, reseller purchase), not
    where an already-paid one finishes. The operator must still exist, and its
    payment credentials must still be active, or we cannot verify the signature
    at all.
    """
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, f"webhook:{provider_key}", limit=60, window_seconds=60)

    raw_body = await request.body()
    operator = (
        await db.execute(
            select(ISPOperator).where(ISPOperator.slug == operator_slug)
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
    # The signature proves only that THIS operator's key signed it. The
    # operator rides with the job, so the payment it names must be this
    # operator's own: another operator's reference is ignored like an unknown one.
    await redis.enqueue_job(
        "process_webhook_event",
        provider_key,
        parsed.internal_reference,
        parsed.status.value,
        parsed.provider_reference,
        raw_body.decode("utf-8"),
        str(operator.id),
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


# ── Arkesel SMS delivery callback ────────────────────────────────────────


def _mask_digits(text: str) -> str:
    # A phone number in a callback (none expected) never reaches the log whole.
    return re.sub(r"(\d{5})\d{4,}(\d{3})", r"\1****\2", text)


def _callback_fields(query: dict, raw_body: bytes, content_type: str) -> dict:
    fields = dict(query)
    if raw_body:
        text = raw_body.decode("utf-8", "replace")
        try:
            body = json.loads(text) if "json" in content_type or text.lstrip().startswith("{") else dict(parse_qsl(text))
        except ValueError:
            body = {}
        if isinstance(body, dict):
            for k, v in body.items():
                fields.setdefault(k, v)
    return fields


@router.api_route("/arkesel/sms-status/{token}", methods=["GET", "POST"])
async def arkesel_sms_status_callback(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Arkesel calls this when a message's status changes, with ?sms_id=&status=
    (its docs; GET or POST not stated, so both are accepted). The callback is
    a trigger only: the token must match the one issued for that message, and
    the status is then read from Arkesel's API by the worker, never taken from
    this request. The raw request is logged (digits masked) so the real format
    can be confirmed."""
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "webhook:arkesel-sms", limit=120, window_seconds=60)
    raw_body = (await request.body())[:4096]
    content_type = request.headers.get("content-type", "")
    logger.info(
        "arkesel_sms_callback method=%s content_type=%s query=%s body=%s ip=%s",
        request.method, content_type[:60], _mask_digits(str(request.url.query))[:500],
        _mask_digits(raw_body.decode("utf-8", "replace"))[:1000], client_ip,
    )
    fields = _callback_fields(dict(request.query_params), raw_body, content_type)
    sms_id = str(fields.get("sms_id") or fields.get("id") or fields.get("message_id") or "")[:64]
    reported = str(fields.get("status") or "")[:32]

    from src.jobs.sms_status import enqueue_status_refresh
    from src.modules.sms.status_tracking import TRACKED_PROVIDERS, token_matches

    row = None
    if sms_id:
        row = (
            await db.execute(
                select(SMSMessage).where(
                    SMSMessage.provider.in_(TRACKED_PROVIDERS), SMSMessage.provider_reference == sms_id
                )
            )
        ).scalar_one_or_none()
    if row is None or not token_matches(token, row.callback_token_hash):
        logger.warning("arkesel_sms_callback_rejected sms_id=%s known=%s ip=%s", sms_id or "-", row is not None, client_ip)
        raise HTTPException(status_code=404, detail="Not found")
    await enqueue_status_refresh(row.id, reported)
    return {"ok": True}
