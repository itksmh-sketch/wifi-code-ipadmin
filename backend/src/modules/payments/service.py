import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ISPOperator, PaymentTransaction, Plan, Voucher
from src.jobs.voucher_sms import enqueue_voucher_sms
from src.modules import audit
from src.modules.audit import stored_text
from src.modules.audit.redact import mask_code
from src.modules.payments.providers.base import PaymentProvider
from src.modules.payments.providers.registry import build_payment_provider
from src.modules.payments.provider_resolver import resolve_active_payment_provider
from src.modules.payments import operator_alerts
from src.modules.payments.types import (
    PROVIDER_UNREACHABLE_STATE,
    TIMEOUT_FAILURE_REASON,
    PaymentMethod,
    PaymentNextAction,
    PaymentStatus,
)
from src.modules.webhooks.urls import build_webhook_url, webhook_base_url
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

    @staticmethod
    async def _record_status_change(
        db: AsyncSession, tx: PaymentTransaction, old_status: str, trigger_source: str, voucher: Voucher | None = None,
        *, late: bool = False,
    ) -> None:
        """The audit row for a payment whose status moved: by the system for a
        webhook, poll or the reconciliation job, or by whoever is signed in when
        a person caused it. In the operator's trail. Never the buyer's phone;
        a voucher only as its masked suffix. Fails open: a payment completes,
        voucher and all, whether or not the audit write succeeds. Caller
        commits."""
        if old_status == tx.status:
            return
        detail = {"internal_reference": tx.internal_reference, "amount_ghs": tx.amount_ghs, "trigger": trigger_source}
        if tx.failure_reason:
            detail["failure_reason"] = tx.failure_reason
        if voucher is not None:
            detail["code_suffix"] = mask_code(voucher.code)
        if late:
            detail["late"] = True
        await audit.record_system_fail_open(
            db, "payment.status_change", isp_operator_id=tx.isp_operator_id, target_type="payment", target_id=tx.id,
            summary=f"Payment {tx.internal_reference} {old_status} -> {tx.status}"
                    + (f", voucher {mask_code(voucher.code)} issued" if voucher is not None else "")
                    + (" (confirmed after it timed out)" if late else ""),
            changes={"status": {"before": old_status, "after": tx.status}}, detail=detail,
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
        # The provider's own words, but never a key it echoed back.
        reason = await stored_text.operator_payment_error(db, tx.isp_operator_id, reason)
        tx.failure_reason = reason
        tx.display_message = reason
        tx.next_action = PaymentNextAction.NONE.value
        tx.provider_state = "initiation_failed"
        tx.completed_at = datetime.now(timezone.utc)
        self._log_status_change(tx, old_status, tx.status, "initiate")
        await self._record_status_change(db, tx, old_status, "initiate")
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
        if tx.status == PaymentStatus.SUCCESS.value:
            return tx
        # A forced re-check (an operator's Re-check, a Flutterwave webhook) may
        # still ask about a payment the reconciliation job timed out.
        if tx.status == PaymentStatus.FAILED.value and not (force and tx.failure_reason == TIMEOUT_FAILURE_REASON):
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
        if tx.status == PaymentStatus.FAILED.value:
            return await self.apply_late_result(db, tx=tx, result=result, trigger_source="poll")
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
            await self._record_status_change(db, tx, old_status, trigger_source)
            await db.commit()
            await db.refresh(tx)
        return tx

    async def apply_provider_result(self, db: AsyncSession, *, tx: PaymentTransaction, result, trigger_source: str) -> PaymentTransaction:
        if result.provider_reference:
            tx.provider_reference = result.provider_reference
        tx.provider_state = result.provider_state
        tx.next_action = result.next_action.value
        tx.display_message = await stored_text.operator_payment_error(db, tx.isp_operator_id, result.display_message)
        tx.payment_channel = result.payment_channel
        if result.provider_payload is not None:
            tx.provider_payload = result.provider_payload
        tx.last_status_check_at = datetime.now(timezone.utc)

        if getattr(result, "authorization_url", None):
            tx.display_message = result.display_message or "Additional authorization is required."
        if result.failure_reason:
            tx.failure_reason = await stored_text.operator_payment_error(db, tx.isp_operator_id, result.failure_reason)

        if result.status == PaymentStatus.SUCCESS:
            await db.commit()
            return await self.resolve_successful_payment(db, internal_reference=tx.internal_reference, trigger_source=trigger_source)

        old_status = tx.status
        tx.status = result.status.value
        if tx.status == PaymentStatus.FAILED.value:
            tx.completed_at = datetime.now(timezone.utc)
            tx.next_action = PaymentNextAction.NONE.value
        self._log_status_change(tx, old_status, tx.status, trigger_source)
        await self._record_status_change(db, tx, old_status, trigger_source)
        await db.commit()
        await db.refresh(tx)
        return tx

    async def apply_late_result(self, db: AsyncSession, *, tx: PaymentTransaction, result, trigger_source: str) -> PaymentTransaction:
        """A provider's answer about a payment the reconciliation job timed out.
        Only a verdict changes it: a success makes it a sale, a failure puts the
        provider's reason in place of ours, which ends the re-checks. No verdict
        (pending, unreachable) leaves it failed and only stamps the check, so it
        is never revived to pending and the portal never shows it as live again."""
        tx.last_status_check_at = datetime.now(timezone.utc)
        if result.provider_state != PROVIDER_UNREACHABLE_STATE and result.status == PaymentStatus.SUCCESS:
            if result.provider_reference:
                tx.provider_reference = result.provider_reference
            tx.payment_channel = result.payment_channel or tx.payment_channel
            if result.provider_payload is not None:
                tx.provider_payload = result.provider_payload
            await db.commit()
            return await self.resolve_successful_payment(db, internal_reference=tx.internal_reference, trigger_source=trigger_source)
        if result.provider_state != PROVIDER_UNREACHABLE_STATE and result.status == PaymentStatus.FAILED:
            tx.failure_reason = (
                await stored_text.operator_payment_error(db, tx.isp_operator_id, result.failure_reason)
                if result.failure_reason else "provider_failed"
            )
            tx.provider_state = result.provider_state
        await db.commit()
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
        send_sms = late = contradicted = False
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

            if tx.status == PaymentStatus.FAILED.value and tx.failure_reason != TIMEOUT_FAILURE_REASON:
                # The provider, or a person, already failed it: a success now
                # contradicts that verdict. Nothing is issued; the operator is
                # asked to check (below, once the row lock is released).
                contradicted = True
            else:
                # Failed only by our two-hour timeout: the customer was charged
                # after we gave up, so it becomes a sale.
                late = tx.status == PaymentStatus.FAILED.value
                await self._issue_voucher(db, tx, trigger_source, late=late)
                send_sms = bool(tx.phone_number)
        finally:
            if tx_ctx:
                await tx_ctx.__aexit__(None, None, None)

        await db.refresh(tx)
        if contradicted:
            logger.warning(
                "payment_success_after_failure internal_reference=%s failure_reason=%s trigger=%s",
                tx.internal_reference, tx.failure_reason, trigger_source,
            )
            await operator_alerts.notify_success_after_failure(tx.id)
            return tx
        if send_sms:
            # Sent by the worker, not here: the provider call used to block this
            # request (the portal's payment-status poll) for up to 10 s. Only the
            # caller that flipped the row to success gets here, so a poll racing
            # a webhook enqueues once; see src/jobs/voucher_sms.py.
            if tx_ctx is None:
                # Joined the caller's transaction: commit so the worker can see
                # the paid row (the onboarding step below commits it anyway).
                await db.commit()
            await enqueue_voucher_sms(tx.id)
        if late:
            await operator_alerts.notify_late_success(tx.id)
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

    async def _issue_voucher(self, db: AsyncSession, tx: PaymentTransaction, trigger_source: str, *, late: bool) -> Voucher:
        """Under the caller's row lock: the voucher a paid transaction buys, and
        the transaction marked paid."""
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
        await self._record_status_change(db, tx, old_status, trigger_source, voucher=voucher, late=late)
        return voucher

    async def provider_for_transaction(self, db: AsyncSession, tx: PaymentTransaction) -> PaymentProvider:
        provider_key, credentials = await resolve_active_payment_provider(db, tx.isp_operator_id)

        operator = await db.get(ISPOperator, tx.isp_operator_id)
        slug = operator.slug if operator else ""
        callback_url = build_webhook_url(await webhook_base_url(), provider_key, slug)
        return build_payment_provider(provider_key, credentials, callback_url=callback_url)
