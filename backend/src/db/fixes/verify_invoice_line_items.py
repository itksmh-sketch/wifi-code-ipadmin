"""Post-migration verification for invoice line items (Phase 2).

Two halves:
  1. The backfill — every pre-existing invoice has lines, and they sum to the
     stored total. Read-only.
  2. The write path — runs the real create_invoice() the cron uses, checks the
     resulting invoice has exactly one subscription line matching its total,
     then ROLLS BACK. Nothing is committed; no invoice or line survives.

    docker exec hotspot-backend python -m src.db.fixes.verify_invoice_line_items
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from sqlalchemy import func, select

from src.db.base import async_session_factory
from src.db.models import ISPOperator, OperatorInvoice, OperatorInvoiceLineItem
from src.modules.billing.service import create_invoice

# Far-future period so a test invoice can never collide with a real one.
# period_start is a timestamptz, so the residue check must compare against a
# real datetime — a bare "2099-01-01" string is sent as VARCHAR and the query
# fails on the type mismatch.
SENTINEL_START = date(2099, 1, 1)
SENTINEL_END = date(2099, 1, 31)
SENTINEL_CUTOFF = datetime(2099, 1, 1, tzinfo=timezone.utc)


async def check_backfill(db) -> int:
    invoices = (await db.execute(select(OperatorInvoice).order_by(OperatorInvoice.invoice_number))).scalars().all()
    if not invoices:
        print("no existing invoices to check")
        return 0

    print(f"Existing invoices ({len(invoices)}):")
    failures = 0
    for inv in invoices:
        lines = (
            await db.execute(
                select(OperatorInvoiceLineItem)
                .where(OperatorInvoiceLineItem.invoice_id == inv.id)
                .order_by(OperatorInvoiceLineItem.sort_order)
            )
        ).scalars().all()
        total = sum((l.amount_ghs for l in lines), Decimal("0"))
        ok = bool(lines) and total == inv.amount_ghs
        failures += 0 if ok else 1
        print(f"  {inv.invoice_number}  status={inv.status:<8} invoice_total=GHS {inv.amount_ghs}  "
              f"lines={len(lines)}  line_sum=GHS {total}  {'OK' if ok else 'MISMATCH'}")
        for l in lines:
            print(f"      [{l.kind}] {l.description!r}  {l.quantity} x {l.unit_price_ghs} = GHS {l.amount_ghs}"
                  f"  sort={l.sort_order}  meta={l.line_metadata}")
    return failures


async def check_write_path(db) -> int:
    operator = (await db.execute(select(ISPOperator).order_by(ISPOperator.name))).scalars().first()
    if operator is None:
        print("no operator to test against")
        return 1

    print(f"\nWrite path — create_invoice() for {operator.name} "
          f"(monthly_fee_ghs = GHS {operator.monthly_fee_ghs}):")
    invoice = await create_invoice(db, operator, SENTINEL_START, SENTINEL_END)
    lines = (
        await db.execute(
            select(OperatorInvoiceLineItem).where(OperatorInvoiceLineItem.invoice_id == invoice.id)
        )
    ).scalars().all()

    failures = 0
    print(f"  invoice {invoice.invoice_number}  amount_ghs=GHS {invoice.amount_ghs}")
    for l in lines:
        print(f"      [{l.kind}] {l.description!r}  {l.quantity} x {l.unit_price_ghs} = GHS {l.amount_ghs}")

    checks = [
        ("exactly one line", len(lines) == 1),
        ("line is a subscription", bool(lines) and lines[0].kind == "subscription"),
        ("line amount == invoice total", bool(lines) and lines[0].amount_ghs == invoice.amount_ghs),
        ("invoice total == operator fee", invoice.amount_ghs == Decimal(operator.monthly_fee_ghs)),
    ]
    for label, ok in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {label}")
        failures += 0 if ok else 1
    return failures


async def _counts(db) -> tuple[int, int]:
    invoices = (await db.execute(select(func.count()).select_from(OperatorInvoice))).scalar() or 0
    lines = (await db.execute(select(func.count()).select_from(OperatorInvoiceLineItem))).scalar() or 0
    return invoices, lines


async def main() -> int:
    async with async_session_factory() as db:
        before = await _counts(db)
        print(f"before: {before[0]} invoice(s), {before[1]} line item(s)\n")

        failures = await check_backfill(db)
        try:
            failures += await check_write_path(db)
        finally:
            # Always, even if the write-path check raised part-way — nothing this
            # script does is kept.
            await db.rollback()

        # Residue check. Comparing counts catches any leak, not just sentinel
        # rows; the sentinel query then names the specific thing we created.
        after = await _counts(db)
        sentinel = (
            await db.execute(
                select(func.count()).select_from(OperatorInvoice).where(
                    OperatorInvoice.period_start >= SENTINEL_CUTOFF
                )
            )
        ).scalar() or 0

        print(f"\nrolled back — after: {after[0]} invoice(s), {after[1]} line item(s)")
        for label, ok in [
            ("invoice count unchanged", after[0] == before[0]),
            ("line item count unchanged", after[1] == before[1]),
            (f"no invoice with period_start >= {SENTINEL_START}", sentinel == 0),
        ]:
            print(f"  {'OK  ' if ok else 'FAIL'} {label}")
            failures += 0 if ok else 1

        print("\n✅ All checks passed." if failures == 0 else f"\n❌ {failures} check(s) failed.")
        return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
