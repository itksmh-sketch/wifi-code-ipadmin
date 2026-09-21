import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ISPOperator, PaymentTransaction, Plan, Voucher
from src.modules.payments.providers.base import PaymentProvider
from src.modules.payments.providers.registry import build_payment_provider
from src.modules.payments.provider_resolver import resolve_active_payment_provider
from src.modules.payments.types import (
    PROVIDER_UNREACHABLE_STATE,
    PaymentMethod,
    PaymentNextAction,
    PaymentStatus,
)
from src.modules.webhooks.urls import build_webhook_url
from src.modules.platform.platform_sms_rate import (
    PlatformSMSRateNotConfigured,
    get_current_platform_sms_rate,
)
from src.modules.sms.metering import record_platform_sms_usage
from src.modules.sms.provider_resolver import resolve_active_sms_provider
from src.modules.sms.providers.registry import build_sms_provider
from src.modules.sms.segmentation import count_sms_segments
from src.modules.sms.service import build_voucher_sms_message

PLATFORM_GATEWAY_PROVIDER_KEY = "arkesel_platform"
from src.modules.vouchers.engine import (
    generate_voucher_code,
    generate_voucher_password,
    generate_voucher_username,
)

logger = logging.getLogger("payments.service")

# Charge states where the provider is waiting on customer-entered input
# (OTP/PIN/phone/birthday/address). Probing the provider's verify endpoint while
# a charge sits in one of these can make the PSP abandon the in-flight
# authorization, so the background/portal poll must not call verify() here - only
# an explicit force (admin action, inbound webhook) may.
_AWAITING_INPUT_ACTIONS = frozenset(
    {
        PaymentNextAction.ENTER_OTP.value,
        PaymentNextAction.ENTER_PIN.value,
        PaymentNextAction.ENTER_PHONE.value,
        PaymentNextAction.ENTER_BIRTHDAY.value,
        PaymentNextAction.ENTER_ADDRESS.value,
    }
)


class PaymentService:
    """Stateless. Which provider brokers a charge is resolved per-transaction
    from the operator's active ``operator_payment_credentials`` row (see
    ``provider_for_transaction``); voucher-delivery SMS likewise resolves from
    the operator's active ``operator_sms_credentials`` row. Nothing
    provider-specific is held on the instance."""

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

    async def _resolve_failed_initiation(
        self, db: AsyncSession, *, tx: PaymentTransaction, reason: str
    ) -> None:
        """The provider rejected the charge before we got a provider reference.
        create_pending_transaction already committed the row, so move it to a
        terminal failed state with the real reason rather than leaving it stuck
        at pending until reconciliation's 2h timeout sweep."""
        if tx.status != PaymentStatus.PENDING.value:
            return
        old_status = tx.status
        tx.status = PaymentStatus.FAILED.value
        tx.failure_reason = reason
        tx.display_message = reason
        tx.next_action = PaymentNextAction.NONE.value
        tx.provider_state = "initiation_failed"
        tx.completed_at = datetime.now(timezone.utc)
        self._log_status_change(tx, old_status, tx.status, "initiate")
        await db.commit()
        await db.refresh(tx)

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
        # Stamp the transaction with the operator's active provider. Raises if
        # they have configured none — surfaced to the portal as a 400.
        provider_key, _ = await resolve_active_payment_provider(db, isp_operator_id)
        tx = PaymentTransaction(
            isp_operator_id=isp_operator_id,
            plan_id=plan_id,
            site_id=site_id,
            amount_ghs=amount_ghs,
            currency="GHS",
            payment_method=payment_method.value,
            provider=provider_key,
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

        try:
            provider = await self.provider_for_transaction(db, tx)
            result = await provider.initiate(
                amount_ghs=Decimal(str(tx.amount_ghs)),
                phone=tx.phone_number,
                plan_id=str(tx.plan_id),
                site_id=str(tx.site_id),
                internal_reference=tx.internal_reference,
                payment_method=tx.payment_method,
                # INET column -> may be an ipaddress object; providers want a string.
                client_ip=str(tx.ip_address) if tx.ip_address is not None else None,
            )
        except ValueError as exc:
            # Provider rejected the charge outright (declined, bad number,
            # unsupported network, misconfigured provider). Resolve the already
            # committed pending row to failed with the real reason, then re-raise
            # so the portal route's existing error handling is unchanged.
            await self._resolve_failed_initiation(db, tx=tx, reason=str(exc))
            raise
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
        if not force and tx.next_action in _AWAITING_INPUT_ACTIONS:
            # The charge is waiting on the customer (OTP/PIN/...). Calling verify()
            # now can knock over the live authorization; let continue_payment
            # drive it, or an explicit force resolve a stale one.
            return tx
        if not force and tx.last_status_check_at:
            age = datetime.now(timezone.utc) - tx.last_status_check_at
            if age.total_seconds() < 5:
                return tx

        provider = await self.provider_for_transaction(db, tx)
        result = await provider.verify(
            tx.provider_reference, expected_amount_ghs=Decimal(str(tx.amount_ghs))
        )
        if result.provider_state == PROVIDER_UNREACHABLE_STATE:
            # No new information. Stamp the check so the debounce above throttles
            # the next poll instead of hammering a provider that is already
            # timing out, and leave the stored state (including a live
            # ENTER_OTP/ENTER_PIN next_action) exactly as it was.
            tx.last_status_check_at = datetime.now(timezone.utc)
            await db.commit()
            return tx
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
                # Already sold and texted to the buyer: must never be printed.
                source="online",
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
        if sms_payload:
            to, code, plan = sms_payload
            resolved = await resolve_active_sms_provider(db, tx.isp_operator_id)
            if resolved is None:
                # No SMS gateway configured for this operator — deliver the
                # voucher on the success page only (matches the old
                # sms_provider='' no-op). Never blocks the payment.
                logger.info(
                    "voucher_sms_skipped operator=%s reason=no_active_sms_provider",
                    tx.isp_operator_id,
                )
            else:
                provider_key, credentials = resolved
                # Resolved BEFORE sending, not after: a send we can't price must
                # not happen at all — never bill for a send we can't confirm,
                # and never send one we can't bill for.
                platform_rate = None
                if provider_key == PLATFORM_GATEWAY_PROVIDER_KEY:
                    try:
                        platform_rate = await get_current_platform_sms_rate(db)
                    except PlatformSMSRateNotConfigured:
                        logger.error(
                            "platform_sms_rate_not_configured operator=%s to=%s — send skipped",
                            tx.isp_operator_id, to,
                        )
                if provider_key == PLATFORM_GATEWAY_PROVIDER_KEY and platform_rate is None:
                    pass  # already logged above
                else:
                    try:
                        # The operator's own template is a nicety; failing to read
                        # it must never cost the buyer their voucher SMS, so this
                        # degrades to the platform default rather than skipping.
                        operator = None
                        try:
                            operator = await db.get(ISPOperator, tx.isp_operator_id)
                        except Exception as exc:
                            logger.warning(
                                "voucher_sms_template_lookup_failed operator=%s error=%s",
                                tx.isp_operator_id, exc,
                            )
                        message = build_voucher_sms_message(
                            code=code,
                            plan=plan,
                            template=getattr(operator, "voucher_sms_template", None),
                            operator_name=getattr(operator, "name", "") or "",
                        )
                        result = await build_sms_provider(provider_key, credentials).send(to=to, message=message)
                        if result is not None and not result.success:
                            logger.error(
                                "voucher_sms_send_failed operator=%s provider=%s to=%s error=%s",
                                tx.isp_operator_id, provider_key, to, result.error,
                            )
                        else:
                            logger.info(
                                "voucher_sms_sent operator=%s provider=%s to=%s",
                                tx.isp_operator_id, provider_key, to,
                            )
                            if provider_key == PLATFORM_GATEWAY_PROVIDER_KEY:
                                segment_info = count_sms_segments(message)
                                amount = (
                                    Decimal(segment_info.segment_count) * platform_rate
                                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                                metered = await record_platform_sms_usage(
                                    isp_operator_id=tx.isp_operator_id,
                                    provider_reference=result.provider_reference if result else None,
                                    segment_count=segment_info.segment_count,
                                    rate_ghs_per_segment=platform_rate,
                                    amount_ghs=amount,
                                )
                                if not metered:
                                    logger.error(
                                        "platform_sms_usage_unrecoverable_after_retries operator=%s to=%s",
                                        tx.isp_operator_id, to,
                                    )
                    except Exception:
                        logger.exception(
                            "voucher_sms_exception operator=%s provider=%s to=%s",
                            tx.isp_operator_id, provider_key, to,
                        )
        try:
            from src.modules.onboarding import mark_checklist
            # A diagnostic payment is a test run, not a sale — the same
            # exclusion get_checklist applies when it derives this step.
            if not tx.is_diagnostic:
                await mark_checklist(db, tx.isp_operator_id, "first_sale_made")
                await db.commit()
        except Exception:
            pass
        return tx

    async def provider_for_transaction(self, db: AsyncSession, tx: PaymentTransaction) -> PaymentProvider:
        provider_key, credentials = await resolve_active_payment_provider(db, tx.isp_operator_id)

        operator = await db.get(ISPOperator, tx.isp_operator_id)
        slug = operator.slug if operator else ""
        callback_url = build_webhook_url(provider_key, slug)
        return build_payment_provider(provider_key, credentials, callback_url=callback_url)
