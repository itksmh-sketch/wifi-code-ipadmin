"""Emails to an operator about a payment that needs a second look.

  * notify_late_success: a payment the reconciliation job timed out was
    confirmed afterwards; the voucher has been issued (and texted, when there
    is a phone number).
  * notify_success_after_failure: a success arrived for a payment that had
    already failed for another reason; nothing was issued.

Fixed wording, not platform templates: every template event has a row seeded
by a migration (tests/test_notification_templates_flow.py), and this change
has none. Move these into template_catalog with the next template migration.

Best effort, like every notification: never raises, logs instead. Sent to the
operator's contact email; a diagnostic payment sends nothing. The buyer's phone
and the full voucher code are never included, only the code's masked suffix.
"""
from __future__ import annotations

import html
import logging
import uuid

from src.db.base import async_session_factory
from src.db.models import ISPOperator, PaymentTransaction, Plan, Voucher
from src.modules.audit.redact import mask_code
from src.modules.notifications.email.service import get_email_service
from src.modules.notifications.template_catalog import wrap_html
from src.modules.platform.settings_service import get_platform_name

logger = logging.getLogger("payments.operator_alerts")


async def _facts(payment_id: uuid.UUID) -> dict | None:
    async with async_session_factory() as db:
        tx = await db.get(PaymentTransaction, payment_id)
        if tx is None or tx.is_diagnostic:
            return None
        operator = await db.get(ISPOperator, tx.isp_operator_id)
        if operator is None or not (operator.contact_email or "").strip():
            return None
        plan = await db.get(Plan, tx.plan_id)
        voucher = await db.get(Voucher, tx.voucher_id) if tx.voucher_id else None
        return {
            "to": operator.contact_email.strip(),
            "isp_name": operator.name,
            "amount": f"GHS {tx.amount_ghs:.2f}",
            "plan": plan.name if plan else "",
            "reference": tx.internal_reference,
            "started": tx.initiated_at.strftime("%d %b %Y, %H:%M GMT") if tx.initiated_at else "",
            "code_suffix": mask_code(voucher.code) if voucher else "",
            "has_phone": bool(tx.phone_number),
            "failure_reason": tx.failure_reason or "",
        }


async def _send(payment_id: uuid.UUID, event: str, compose) -> None:
    try:
        facts = await _facts(payment_id)
        if facts is None:
            return
        facts["platform_name"] = await get_platform_name()
        subject, paragraphs = compose(facts)
        text = "\n\n".join(paragraphs)
        body = "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)
        await get_email_service().send(to=facts["to"], subject=subject, body_html=wrap_html(body), body_text=text)
    except Exception as exc:
        logger.error("payment_operator_alert_failed event=%s payment=%s error=%s", event, payment_id, exc)


def _details(f: dict) -> str:
    lines = [f"Amount: {f['amount']}", f"Plan: {f['plan']}", f"Reference: {f['reference']}", f"Started: {f['started']}"]
    if f["code_suffix"]:
        lines.append(f"Voucher: {f['code_suffix']}")
    return "\n".join(lines)


def _late_success(f: dict) -> tuple[str, list[str]]:
    delivery = (
        "We have issued the voucher and sent it to the customer by SMS."
        if f["has_phone"] else
        "We have issued the voucher. There is no phone number on this payment, so we could not text it: "
        "find it under Vouchers in your dashboard and give it to the customer if they contact you."
    )
    return f"Payment confirmed late: {f['amount']}", [
        f"Hello {f['isp_name']},",
        "A customer's payment was confirmed by the payment provider after it had been marked failed "
        "(no confirmation within 2 hours).",
        _details(f),
        delivery,
        f"— {f['platform_name']}",
    ]


def _success_after_failure(f: dict) -> tuple[str, list[str]]:
    return f"Please check a payment: {f['amount']}", [
        f"Hello {f['isp_name']},",
        "The payment provider reported this payment as successful, but it had already failed "
        f"({f['failure_reason']}). No voucher was issued.",
        _details(f),
        "Please look the payment up in your payment provider's dashboard. If the customer was charged, "
        "give them a voucher or refund them.",
        f"— {f['platform_name']}",
    ]


async def notify_late_success(payment_id: uuid.UUID) -> None:
    await _send(payment_id, "late_success", _late_success)


async def notify_success_after_failure(payment_id: uuid.UUID) -> None:
    await _send(payment_id, "success_after_failure", _success_after_failure)
