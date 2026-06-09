from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import OperatorPaymentCredential
from src.middleware.auth import TenantContext, get_admin_tenant_context
from src.schemas import PaymentCredentialResponse, PaymentCredentialUpdate
from src.utils.encryption import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/payment-credentials", tags=["payment-credentials"])


def _last4(value: str | None) -> str | None:
    return value[-4:] if value else None


def _validate_paystack_keys(public_key: str, secret_key: str) -> None:
    if not (public_key.startswith("pk_test_") or public_key.startswith("pk_live_")):
        raise HTTPException(status_code=400, detail="Paystack public key must start with pk_test_ or pk_live_")
    if not (secret_key.startswith("sk_test_") or secret_key.startswith("sk_live_")):
        raise HTTPException(status_code=400, detail="Paystack secret key must start with sk_test_ or sk_live_")


async def _get_credentials(db: AsyncSession, operator_id):
    return (
        await db.execute(
            select(OperatorPaymentCredential).where(OperatorPaymentCredential.isp_operator_id == operator_id)
        )
    ).scalar_one_or_none()


def _response_from_credentials(creds: OperatorPaymentCredential | None) -> PaymentCredentialResponse:
    if not creds:
        return PaymentCredentialResponse()
    public_key = decrypt_secret(creds.public_key_encrypted)
    secret_key = decrypt_secret(creds.secret_key_encrypted)
    webhook_secret = decrypt_secret(creds.webhook_secret_encrypted) if creds.webhook_secret_encrypted else None
    return PaymentCredentialResponse(
        provider=creds.provider,
        public_key_last4=_last4(public_key),
        secret_key_last4=_last4(secret_key),
        webhook_secret_last4=_last4(webhook_secret),
        is_active=bool(creds.is_active),
        is_configured=True,
        last_validated_at=creds.last_validated_at,
        last_validation_error=creds.last_validation_error,
    )


@router.get("", response_model=PaymentCredentialResponse)
async def get_payment_credentials(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    return _response_from_credentials(await _get_credentials(db, tenant.isp_operator_id))


@router.put("", response_model=PaymentCredentialResponse)
async def update_payment_credentials(
    body: PaymentCredentialUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    _validate_paystack_keys(body.public_key, body.secret_key)
    creds = await _get_credentials(db, tenant.isp_operator_id)
    if not creds:
        creds = OperatorPaymentCredential(
            isp_operator_id=tenant.isp_operator_id,
            provider=body.provider.value,
            public_key_encrypted=encrypt_secret(body.public_key),
            secret_key_encrypted=encrypt_secret(body.secret_key),
            webhook_secret_encrypted=encrypt_secret(body.webhook_secret) if body.webhook_secret else None,
            is_active=body.is_active,
        )
        db.add(creds)
    else:
        creds.provider = body.provider.value
        creds.public_key_encrypted = encrypt_secret(body.public_key)
        creds.secret_key_encrypted = encrypt_secret(body.secret_key)
        creds.webhook_secret_encrypted = encrypt_secret(body.webhook_secret) if body.webhook_secret else None
        creds.is_active = body.is_active
        creds.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(creds)
    from src.modules.onboarding import mark_checklist
    await mark_checklist(db, tenant.isp_operator_id, "payment_configured")
    await db.commit()
    return _response_from_credentials(creds)


@router.post("/test", response_model=PaymentCredentialResponse)
async def test_payment_credentials(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_admin_tenant_context),
):
    creds = await _get_credentials(db, tenant.isp_operator_id)
    if not creds:
        raise HTTPException(status_code=404, detail="Payment credentials are not configured")
    secret_key = decrypt_secret(creds.secret_key_encrypted)
    try:
        async with httpx.AsyncClient(timeout=15.0, base_url="https://api.paystack.co") as client:
            response = await client.get("/transaction", params={"perPage": 1}, headers={"Authorization": f"Bearer {secret_key}"})
            response.raise_for_status()
        creds.last_validated_at = datetime.now(timezone.utc)
        creds.last_validation_error = None
    except Exception as exc:
        creds.last_validation_error = str(exc)
        await db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(creds)
    return _response_from_credentials(creds)
