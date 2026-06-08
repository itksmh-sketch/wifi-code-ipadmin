import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.models import ISPOperator, OperatorPaymentCredential, PaymentTransaction, Plan, Voucher
from src.modules.payments.providers.paystack import PaystackProvider
from src.modules.payments.providers.base import PaymentProvider
from src.modules.payments.types import PaymentMethod, PaymentNextAction, PaymentProviderName, PaymentStatus
from src.modules.sms.service import SMSService, build_voucher_sms_message
from src.modules.vouchers.engine import (
    generate_voucher_code,
    generate_voucher_password,
    generate_voucher_username,
)
from src.utils.encryption import decrypt_secret

logger = logging.getLogger("payments.service")


class PaymentService:
    def __init__(
        self,
        mtn_provider: PaymentProvider,
        vodafone_provider: PaymentProvider,
        airteltigo_provider: PaymentProvider,
        paystack_provider: PaymentProvider,
        sms_service: SMSService | None = None,
    ) -> None:
        # Paystack is the only active provider. Payment method still captures the
        # customer-selected network/channel, but every flow is brokered by Paystack.
        self._providers = {
            PaymentMethod.MTN_MOMO: paystack_provider,
            PaymentMethod.VODAFONE_CASH: paystack_provider,
            PaymentMethod.AIRTELTIGO: paystack_provider,
            PaymentMethod.CARD: paystack_provider,
        }
        self._provider_names = {
            PaymentMethod.MTN_MOMO: PaymentProviderName.PAYSTACK.value,
            PaymentMethod.VODAFONE_CASH: PaymentProviderName.PAYSTACK.value,
            PaymentMethod.AIRTELTIGO: PaymentProviderName.PAYSTACK.value,
            PaymentMethod.CARD: PaymentProviderName.PAYSTACK.value,
        }
        self._sms_service = sms_service

    @staticmethod
    def generate_internal_reference() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def normalize_phone(phone_number: str) -> str:
        digits = re.sub(r"\D", "", phone_number or "")
        if digits.startswith("0") and len(digits) == 10:
            return f"233{digits[1:]}"
        if digits.startswith("233") and len(digits) == 12:
            return digits
        raise ValueError("Invalid Ghana phone number format")

    def provider_for_method(self, payment_method: PaymentMethod) -> PaymentProvider:
        provider = self._providers.get(payment_method)
        if not provider:
            raise ValueError(f"Unsupported payment method: {payment_method}")
        return provider

    @staticmethod
    def _log_status_change(transaction: PaymentTransaction, old_status: str, new_status: str, trigger_source: str) -> None:
        logger.info(
            "payment_status_change timestamp=%s payment_id=%s reference=%s old=%s new=%s trigger=%s",
            datetime.now(timezone.utc).isoformat(),
            transaction.id,
            transaction.internal_reference,
            old_status,
            new_status,
            trigger_source,
        )

    async def create_pending_transaction(
        self,
        db: AsyncSession,
        *,
        plan_id: str,
        site_id: str,
        isp_operator_id: str,
        amount_ghs: Decimal,
        payment_method: PaymentMethod,
        phone_number: str | None,
        ip_address: str | None,
    ) -> PaymentTransaction:
        internal_reference = self.generate_internal_reference()
        tx = PaymentTransaction(
            isp_operator_id=isp_operator_id,
            plan_id=plan_id,
            site_id=site_id,
            amount_ghs=amount_ghs,
            currency="GHS",
            payment_method=payment_method.value,
            provider=self._provider_names[payment_method],
            internal_reference=internal_reference,
            phone_number=self.normalize_phone(phone_number) if phone_number else None,
            status=PaymentStatus.PENDING.value,
            initiated_at=datetime.now(timezone.utc),
            ip_address=ip_address,
            next_action=PaymentNextAction.WAIT.value,
            provider_state="created",
            display_message="Starting payment request...",
        )
        db.add(tx)
        await db.commit()
        await db.refresh(tx)
        return tx

    async def initiate_payment(
        self,
        db: AsyncSession,
        *,
        transaction_id: str,
    ) -> PaymentTransaction:
        tx = await db.get(PaymentTransaction, transaction_id)
        if not tx:
            raise ValueError("Payment transaction not found")
        if tx.status != PaymentStatus.PENDING.value:
            return tx

        provider = await self.provider_for_transaction(db, tx)
        result = await provider.initiate(
            amount_ghs=Decimal(str(tx.amount_ghs)),
            phone=tx.phone_number,
            plan_id=str(tx.plan_id),
            site_id=str(tx.site_id),
            internal_reference=tx.internal_reference,
            payment_method=tx.payment_method,
        )
        return await self.apply_provider_result(db, tx=tx, result=result, trigger_source="initiate")

    async def continue_payment(
        self,
        db: AsyncSession,
        *,
        internal_reference: str,
        otp: str | None = None,
        phone: str | None = None,
        pin: str | None = None,
        birthday: str | None = None,
        address: str | None = None,
        city: str | None = None,
        state: str | None = None,
        zip_code: str | None = None,
    ) -> PaymentTransaction:
        tx = await self.get_transaction_by_reference(db, internal_reference)
        if tx.status in {PaymentStatus.SUCCESS.value, PaymentStatus.FAILED.value}:
            return tx

        provider = await self.provider_for_transaction(db, tx)
        action = tx.next_action or PaymentNextAction.WAIT.value
        if action == PaymentNextAction.ENTER_OTP.value:
            if not otp:
                raise ValueError("OTP is required")
            result = await provider.submit_otp(tx.provider_reference or tx.internal_reference, otp)
        elif action == PaymentNextAction.ENTER_PHONE.value:
            if not phone:
                raise ValueError("Phone number is required")
            normalized_phone = self.normalize_phone(phone)
            tx.phone_number = normalized_phone
            result = await provider.submit_phone(tx.provider_reference or tx.internal_reference, normalized_phone)
        elif action == PaymentNextAction.ENTER_PIN.value:
            if not pin:
                raise ValueError("PIN is required")
            result = await provider.submit_pin(tx.provider_reference or tx.internal_reference, pin)
        elif action == PaymentNextAction.ENTER_BIRTHDAY.value:
            if not birthday:
                raise ValueError("Birthday is required")
            result = await provider.submit_birthday(tx.provider_reference or tx.internal_reference, birthday)
        elif action == PaymentNextAction.ENTER_ADDRESS.value:
            if not all([address, city, state, zip_code]):
                raise ValueError("Address, city, state, and zip code are required")
            result = await provider.submit_address(
                tx.provider_reference or tx.internal_reference,
                address=address,
                city=city,
                state=state,
                zip_code=zip_code,
            )
        else:
            raise ValueError("This payment does not require additional input right now")
        return await self.apply_provider_result(db, tx=tx, result=result, trigger_source="continue")

    async def refresh_transaction_status(
        self,
        db: AsyncSession,
        *,
        tx: PaymentTransaction,
        force: bool = False,
    ) -> PaymentTransaction:
        if tx.status in {PaymentStatus.SUCCESS.value, PaymentStatus.FAILED.value}:
            return tx
        if not tx.provider_reference:
            return tx
        if not force and tx.last_status_check_at:
            age = datetime.now(timezone.utc) - tx.last_status_check_at
            if age.total_seconds() < 5:
                return tx

        provider = await self.provider_for_transaction(db, tx)
        result = await provider.verify(tx.provider_reference)
        return await self.apply_provider_result(db, tx=tx, result=result, trigger_source="poll")

    async def apply_webhook_update(
        self,
        db: AsyncSession,
        *,
        tx: PaymentTransaction,
        status: PaymentStatus,
        provider_reference: str | None,
        provider_state: str | None,
        display_message: str | None,
        provider_payload: dict | None,
        payment_channel: str | None,
        trigger_source: str,
    ) -> PaymentTransaction:
        if provider_payload is not None:
            tx.webhook_payload = provider_payload
        if provider_reference:
            tx.provider_reference = provider_reference
        if provider_state:
            tx.provider_state = provider_state
        if display_message:
            tx.display_message = display_message
        if payment_channel:
            tx.payment_channel = payment_channel
        tx.last_status_check_at = datetime.now(timezone.utc)
        tx.provider_payload = provider_payload or tx.provider_payload
        tx.next_action = PaymentNextAction.NONE.value if status != PaymentStatus.PENDING else tx.next_action
        await db.commit()

        if status == PaymentStatus.SUCCESS:
            return await self.resolve_successful_payment(db, internal_reference=tx.internal_reference, trigger_source=trigger_source)
        if status == PaymentStatus.FAILED and tx.status == PaymentStatus.PENDING.value:
            old_status = tx.status
            tx.status = PaymentStatus.FAILED.value
            tx.failure_reason = "provider_failed"
            tx.completed_at = datetime.now(timezone.utc)
            tx.next_action = PaymentNextAction.NONE.value
            self._log_status_change(tx, old_status, tx.status, trigger_source)
            await db.commit()
            await db.refresh(tx)
        return tx

    async def apply_provider_result(self, db: AsyncSession, *, tx: PaymentTransaction, result, trigger_source: str) -> PaymentTransaction:
        if result.provider_reference:
            tx.provider_reference = result.provider_reference
        tx.provider_state = result.provider_state
        tx.next_action = result.next_action.value
        tx.display_message = result.display_message
        tx.payment_channel = result.payment_channel
        if result.provider_payload is not None:
            tx.provider_payload = result.provider_payload
        tx.last_status_check_at = datetime.now(timezone.utc)

        if getattr(result, "authorization_url", None):
            tx.display_message = result.display_message or "Additional authorization is required."
        if result.failure_reason:
            tx.failure_reason = result.failure_reason

        if result.status == PaymentStatus.SUCCESS:
            await db.commit()
            return await self.resolve_successful_payment(db, internal_reference=tx.internal_reference, trigger_source=trigger_source)

        old_status = tx.status
        tx.status = result.status.value
        if tx.status == PaymentStatus.FAILED.value:
            tx.completed_at = datetime.now(timezone.utc)
            tx.next_action = PaymentNextAction.NONE.value
        self._log_status_change(tx, old_status, tx.status, trigger_source)
        await db.commit()
        await db.refresh(tx)
        return tx

    async def get_transaction_by_reference(self, db: AsyncSession, internal_reference: str) -> PaymentTransaction:
        result = await db.execute(select(PaymentTransaction).where(PaymentTransaction.internal_reference == internal_reference))
        tx = result.scalar_one_or_none()
        if not tx:
            raise ValueError("Payment transaction not found")
        return tx

    async def resolve_successful_payment(
        self,
        db: AsyncSession,
        *,
        internal_reference: str,
        trigger_source: str,
    ) -> PaymentTransaction:
        sms_payload: tuple[str, str, Plan] | None = None
        tx_ctx = db.begin() if not db.in_transaction() else None
        if tx_ctx:
            await tx_ctx.__aenter__()
        try:
            result = await db.execute(
                select(PaymentTransaction)
                .where(PaymentTransaction.internal_reference == internal_reference)
                .with_for_update()
            )
            tx = result.scalar_one_or_none()
            if not tx:
                raise ValueError("Payment transaction not found")

            if tx.status == PaymentStatus.SUCCESS.value:
                return tx

            if tx.status == PaymentStatus.FAILED.value:
                return tx

            plan_result = await db.execute(select(Plan).where(Plan.id == tx.plan_id, Plan.isp_operator_id == tx.isp_operator_id))
            plan = plan_result.scalar_one_or_none()
            if not plan:
                raise ValueError("Plan not found for payment transaction")

            voucher = Voucher(
                isp_operator_id=tx.isp_operator_id,
                plan_id=tx.plan_id,
                site_id=tx.site_id,
                code=generate_voucher_code(),
                username=generate_voucher_username(),
                password=generate_voucher_password(),
                status="unused",
                device_policy="single",
                max_devices=1,
                expires_at=None,
                batch_id=f"PAY-{tx.internal_reference[:8]}",
            )
            db.add(voucher)
            await db.flush()

            tx.voucher_id = voucher.id
            old_status = tx.status
            tx.status = PaymentStatus.SUCCESS.value
            tx.completed_at = datetime.now(timezone.utc)
            tx.failure_reason = None
            tx.next_action = PaymentNextAction.NONE.value
            tx.provider_state = "success"
            tx.display_message = "Payment successful."
            self._log_status_change(tx, old_status, tx.status, trigger_source)

            if tx.phone_number:
                sms_payload = (tx.phone_number, voucher.code, plan)
        finally:
            if tx_ctx:
                await tx_ctx.__aexit__(None, None, None)

        await db.refresh(tx)
        if sms_payload and self._sms_service and self._sms_service.enabled:
            to, code, plan = sms_payload
            try:
                message = build_voucher_sms_message(code=code, plan=plan)
                result = await self._sms_service.send(to=to, message=message)
                if result is not None and not result.success:
                    logger.error(
                        "sms_send_failed provider=%s to=%s error=%s",
                        (self._sms_service.settings.sms_provider or ""),
                        to,
                        result.error,
                    )
            except Exception:
                logger.exception(
                    "sms_send_exception provider=%s to=%s",
                    (self._sms_service.settings.sms_provider or ""),
                    to,
                )
        try:
            from src.modules.onboarding import mark_checklist
            await mark_checklist(db, tx.isp_operator_id, "first_sale_made")
            await db.commit()
        except Exception:
            pass
        return tx

    async def provider_for_transaction(self, db: AsyncSession, tx: PaymentTransaction) -> PaymentProvider:
        payment_method = PaymentMethod(tx.payment_method)
        if self._provider_names[payment_method] != PaymentProviderName.PAYSTACK.value:
            return self.provider_for_method(payment_method)

        creds = (
            await db.execute(
                select(OperatorPaymentCredential).where(
                    OperatorPaymentCredential.isp_operator_id == tx.isp_operator_id,
                    OperatorPaymentCredential.provider == PaymentProviderName.PAYSTACK.value,
                    OperatorPaymentCredential.is_active == True,
                )
            )
        ).scalar_one_or_none()
        if not creds:
            raise ValueError("Operator has not configured payment credentials")

        operator = await db.get(ISPOperator, tx.isp_operator_id)
        slug = operator.slug if operator else ""
        webhook_base = get_settings().webhook_base_url.rstrip("/")
        callback_url = f"{webhook_base}/api/v1/webhooks/paystack/{slug}" if webhook_base and slug else None
        return PaystackProvider(
            secret_key=decrypt_secret(creds.secret_key_encrypted),
            public_key=decrypt_secret(creds.public_key_encrypted),
            webhook_secret=decrypt_secret(creds.webhook_secret_encrypted) if creds.webhook_secret_encrypted else None,
            callback_url=callback_url,
        )
