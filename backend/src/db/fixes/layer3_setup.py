"""Layer 3 setup — a disposable operator with a real, payable Paystack test charge.

Creates the operator and one GHS 5.00 invoice, then calls the real
initiate_invoice_payment() so the checkout URL comes from the production code
path, not a shortcut. Paystack then sends a genuine signed webhook when the
charge completes.

    docker exec hotspot-backend python -m src.db.fixes.layer3_setup          # create
    docker exec hotspot-backend python -m src.db.fixes.layer3_setup --status # check
    docker exec hotspot-backend python -m src.db.fixes.layer3_setup --cleanup
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from sqlalchemy import delete, select

from src.db.base import async_session_factory
from src.db.models import AdminUser, ISPOperator, OperatorBillingEvent, OperatorInvoice
from src.modules.billing.service import create_invoice
from src.utils.auth import hash_password

SLUG = "layer3-test"
FEE = Decimal("5.00")
# Disposable throwaway credentials for one manual test; deleted by --cleanup.
ADMIN_EMAIL = "layer3-admin@example.com"
ADMIN_PASSWORD = "Layer3Test!2026"


async def create() -> int:
    async with async_session_factory() as db:
        if (await db.execute(select(ISPOperator).where(ISPOperator.slug == SLUG))).scalar_one_or_none():
            print(f"Operator {SLUG!r} already exists — run --cleanup first.")
            return 1
        operator = ISPOperator(
            name="Layer 3 Test", slug=SLUG,
            contact_email="layer3-test@example.com", contact_phone="0000000000",
            status="approved", billing_status="active",
            monthly_fee_ghs=FEE, approved_at=datetime.now(timezone.utc),
        )
        db.add(operator)
        await db.flush()
        db.add(AdminUser(
            isp_operator_id=operator.id,
            email=ADMIN_EMAIL,
            password_hash=hash_password(ADMIN_PASSWORD),
            role="superadmin",
            is_active=True,
        ))
        invoice = await create_invoice(db, operator, date(2026, 9, 1), date(2026, 9, 30))
        await db.commit()
        op_id, inv_id, inv_no = operator.id, invoice.id, invoice.invoice_number

    # Deliberately NOT pre-initiating payment: the operator clicks Pay in the
    # admin UI, so the test covers the real operator-facing path. Initiating here
    # too would burn the INV-{uuid} reference and Paystack rejects a reuse.
    print(f"operator   : {SLUG}  ({op_id})")
    print(f"invoice    : {inv_no}  GHS {FEE}  (500 pesewas expected)")
    print(f"reference  : INV-{inv_id}")
    print(f"\nlogin at /admin/  ->  {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
    print("then Billing -> Pay for the invoice\n")
    return 0


async def new_invoice() -> int:
    """Add another invoice to the existing test operator.

    Each invoice gets a fresh UUID, hence a fresh INV-{uuid} Paystack reference —
    a paid invoice's reference cannot be reused, so retrying payment needs a new
    invoice rather than another attempt at the old one.
    """
    async with async_session_factory() as db:
        operator = (await db.execute(select(ISPOperator).where(ISPOperator.slug == SLUG))).scalar_one_or_none()
        if not operator:
            print("no layer3 operator — run without flags first")
            return 1
        n = len((await db.execute(
            select(OperatorInvoice).where(OperatorInvoice.isp_operator_id == operator.id)
        )).scalars().all())
        invoice = await create_invoice(db, operator, date(2026, 10, 1), date(2026, 10, 31))
        await db.commit()
        print(f"new invoice : {invoice.invoice_number}  GHS {invoice.amount_ghs}  (was {n} invoice(s))")
        print(f"reference   : INV-{invoice.id}")
        print(f"\nlog in at https://ip-admin.duckdns.org/admin/  ->  {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
        print(f"Billing -> Pay for {invoice.invoice_number}\n")
    return 0


async def status() -> int:
    async with async_session_factory() as db:
        operator = (await db.execute(select(ISPOperator).where(ISPOperator.slug == SLUG))).scalar_one_or_none()
        if not operator:
            print("no layer3 operator present")
            return 1
        invoices = (await db.execute(
            select(OperatorInvoice).where(OperatorInvoice.isp_operator_id == operator.id)
        )).scalars().all()
        events = (await db.execute(
            select(OperatorBillingEvent).where(OperatorBillingEvent.isp_operator_id == operator.id)
            .order_by(OperatorBillingEvent.created_at)
        )).scalars().all()

    print(f"operator: status={operator.status} billing_status={operator.billing_status} "
          f"suspension_reason={operator.suspension_reason}")
    for i in invoices:
        print(f"  {i.invoice_number}: status={i.status} amount=GHS {i.amount_ghs} "
              f"paid_at={i.paid_at} reference={i.payment_reference}")
    print("  events:")
    for e in events:
        print(f"    [{e.event_type}] {e.description}")
        if e.event_type in ("invoice_paid", "payment_rejected"):
            print(f"       {e.event_metadata}")
    return 0


async def cleanup() -> int:
    async with async_session_factory() as db:
        operator = (await db.execute(select(ISPOperator).where(ISPOperator.slug == SLUG))).scalar_one_or_none()
        if not operator:
            print("nothing to clean up")
            return 0
        op_id = operator.id
        await db.execute(delete(AdminUser).where(AdminUser.isp_operator_id == op_id))
        await db.execute(delete(OperatorBillingEvent).where(OperatorBillingEvent.isp_operator_id == op_id))
        await db.execute(delete(OperatorInvoice).where(OperatorInvoice.isp_operator_id == op_id))
        await db.execute(delete(ISPOperator).where(ISPOperator.id == op_id))
        await db.commit()
        print(f"removed operator {op_id} with its admin user, invoices, line items and events")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--new-invoice", action="store_true")
    ap.add_argument("--cleanup", action="store_true")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(
        status() if a.status else cleanup() if a.cleanup
        else new_invoice() if a.new_invoice else create()))
