"""Layer 2 — real signed HTTP round trips against the live webhook endpoint.

Each request is signed with the genuine configured secret, so it traverses the
real signature check, the real handler and the real database. Only the payload
*content* is synthetic — which is the point: Paystack will not produce an
underpayment or a wrong-currency charge on demand.

The webhook commits, so this cannot run inside a rollback. It creates a
disposable operator and its invoices, and deletes them in a finally block.

    docker exec hotspot-backend python -m src.db.fixes.layer2_webhook_verification --apply
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import httpx
from sqlalchemy import delete, func, select

from src.db.base import async_session_factory
from src.db.models import ISPOperator, OperatorBillingEvent, OperatorInvoice, OperatorInvoiceLineItem
from src.modules.billing.service import create_invoice, ghs_to_pesewas
from src.modules.platform.payment_credentials_service import resolve_paystack_keys

URL = "http://localhost:8000/api/v1/webhooks/platform-billing/paystack"
SLUG = "webhook-test"
FEE = Decimal("5.00")

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {detail}")


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()


async def post(payload: dict, secret: str) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            URL, content=body,
            headers={"Content-Type": "application/json", "x-paystack-signature": sign(body, secret)},
        )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text}


def charge(invoice_id, amount_pesewas, *, currency="GHS", include_metadata=True, operator_id=None):
    data = {
        "amount": amount_pesewas,
        "currency": currency,
        "reference": f"INV-{invoice_id}",
        "status": "success",
    }
    if include_metadata:
        data["metadata"] = {"invoice_id": str(invoice_id), "operator_id": str(operator_id)}
    return {"event": "charge.success", "data": data}


async def fresh(op_id):
    """Read committed state in a new session — the handler used a different one."""
    async with async_session_factory() as db:
        op = (await db.execute(select(ISPOperator).where(ISPOperator.id == op_id))).scalar_one()
        return op.status, op.billing_status, op.suspension_reason


async def invoice_state(inv_id):
    async with async_session_factory() as db:
        inv = (await db.execute(select(OperatorInvoice).where(OperatorInvoice.id == inv_id))).scalar_one()
        return inv.status, inv.paid_at, inv.payment_reference


async def rejection_events(op_id) -> int:
    async with async_session_factory() as db:
        return (await db.execute(
            select(func.count()).select_from(OperatorBillingEvent).where(
                OperatorBillingEvent.isp_operator_id == op_id,
                OperatorBillingEvent.event_type == "payment_rejected",
            )
        )).scalar() or 0


async def set_operator(op_id, *, status, billing_status, suspension_reason):
    async with async_session_factory() as db:
        op = (await db.execute(select(ISPOperator).where(ISPOperator.id == op_id))).scalar_one()
        op.status, op.billing_status, op.suspension_reason = status, billing_status, suspension_reason
        await db.commit()


async def cleanup(op_id) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(OperatorBillingEvent).where(OperatorBillingEvent.isp_operator_id == op_id))
        await db.execute(delete(OperatorInvoice).where(OperatorInvoice.isp_operator_id == op_id))
        await db.execute(delete(ISPOperator).where(ISPOperator.id == op_id))
        await db.commit()


async def main(apply: bool) -> int:
    async with async_session_factory() as db:
        keys = await resolve_paystack_keys(db)
    secret = keys.webhook_secret or keys.secret_key
    if not secret.strip():
        print("No signing secret resolved — configure test keys first.")
        return 1
    if not apply:
        print("Dry run. Re-run with --apply.")
        return 0

    # --- setup -------------------------------------------------------------
    async with async_session_factory() as db:
        existing = (await db.execute(select(ISPOperator).where(ISPOperator.slug == SLUG))).scalar_one_or_none()
        if existing:
            print(f"Refusing to run: an operator with slug {SLUG!r} already exists.")
            return 1
        operator = ISPOperator(
            name="Webhook Test", slug=SLUG,
            contact_email="webhook-test@example.invalid", contact_phone="0000000000",
            status="approved", billing_status="active",
            monthly_fee_ghs=FEE, approved_at=datetime.now(timezone.utc),
        )
        db.add(operator)
        await db.flush()
        op_id = operator.id
        invs = []
        for m in range(1, 7):
            inv = await create_invoice(db, operator, date(2026, m, 1), date(2026, m, 28))
            invs.append((inv.id, inv.invoice_number))
        await db.commit()

    expected = ghs_to_pesewas(FEE)
    print(f"\ntest operator {SLUG} ({op_id}) fee GHS {FEE}; invoices "
          f"{', '.join(n for _, n in invs)}; expected {expected} pesewas\n")

    try:
        # --- 1. underpayment ------------------------------------------------
        print("1. Underpayment — GHS 1.00 against a GHS 5.00 invoice")
        inv_id, inv_no = invs[0]
        before = await rejection_events(op_id)
        code, body = await post(charge(inv_id, 100, operator_id=op_id), secret)
        st, paid_at, _ = await invoice_state(inv_id)
        after = await rejection_events(op_id)
        record("underpayment", st != "paid" and paid_at is None and after == before + 1,
               f"{inv_no}: HTTP {code} {body.get('message')!r} -> status={st} paid_at={paid_at} "
               f"payment_rejected_events={before}->{after}")

        # --- 2. wrong currency ----------------------------------------------
        print("2. Wrong currency — correct amount, NGN")
        inv_id, inv_no = invs[1]
        before = await rejection_events(op_id)
        code, body = await post(charge(inv_id, expected, currency="NGN", operator_id=op_id), secret)
        st, paid_at, _ = await invoice_state(inv_id)
        after = await rejection_events(op_id)
        record("wrong currency", st != "paid" and paid_at is None and after == before + 1,
               f"{inv_no}: HTTP {code} {body.get('message')!r} -> status={st} paid_at={paid_at} "
               f"payment_rejected_events={before}->{after}")

        # --- 3. metadata stripped, reference fallback -----------------------
        print("3. Metadata stripped — must be found via the INV-{uuid} reference")
        inv_id, inv_no = invs[2]
        code, body = await post(charge(inv_id, expected, include_metadata=False), secret)
        st, paid_at, ref = await invoice_state(inv_id)
        record("reference fallback", st == "paid" and paid_at is not None,
               f"{inv_no}: HTTP {code} {body.get('message')!r} -> status={st} reference={ref}")

        # --- 4. past_due but NOT suspended pays -----------------------------
        print("4. past_due, not suspended, pays — billing_status must normalise")
        inv_id, inv_no = invs[3]
        await set_operator(op_id, status="approved", billing_status="past_due", suspension_reason=None)
        code, body = await post(charge(inv_id, expected, operator_id=op_id), secret)
        st, _, _ = await invoice_state(inv_id)
        o_st, o_bs, o_sr = await fresh(op_id)
        record("billing_status normalised", st == "paid" and o_bs == "active" and o_st == "approved",
               f"{inv_no}: paid={st=='paid'} -> status={o_st} billing_status=past_due->{o_bs} reason={o_sr}")

        # --- 5. manually suspended pays -------------------------------------
        print("5. Manually suspended (abuse) pays — must NOT be reactivated")
        inv_id, inv_no = invs[4]
        await set_operator(op_id, status="suspended", billing_status="past_due", suspension_reason="manual")
        code, body = await post(charge(inv_id, expected, operator_id=op_id), secret)
        st, _, _ = await invoice_state(inv_id)
        o_st, o_bs, o_sr = await fresh(op_id)
        record("manual suspension survives payment",
               st == "paid" and o_st == "suspended" and o_sr == "manual",
               f"{inv_no}: paid={st=='paid'} -> status={o_st} (must stay suspended) "
               f"billing_status={o_bs} reason={o_sr}")

        # --- 6. positive control: billing suspension IS lifted ---------------
        print("6. Positive control — billing suspension pays and IS reactivated")
        inv_id, inv_no = invs[5]
        await set_operator(op_id, status="suspended", billing_status="past_due", suspension_reason="billing")
        code, body = await post(charge(inv_id, expected, operator_id=op_id), secret)
        st, _, _ = await invoice_state(inv_id)
        o_st, o_bs, o_sr = await fresh(op_id)
        record("billing suspension lifted",
               st == "paid" and o_st == "approved" and o_bs == "active" and o_sr is None,
               f"{inv_no}: paid={st=='paid'} -> status=suspended->{o_st} billing_status={o_bs} "
               f"reason=billing->{o_sr}")
    finally:
        print("\ncleaning up…")
        await cleanup(op_id)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{'✅ All 6 scenarios passed.' if not failed else '❌ FAILED: ' + ', '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    raise SystemExit(asyncio.run(main(ap.parse_args().apply)))
