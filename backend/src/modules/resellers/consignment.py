"""Resellers on consignment (migration 060): allocating codes, reclaiming the
unsold ones, what each code is now, and the running account.

Every function takes the operator id and scopes every query by it, so an
operator's routes and a reseller's own routes share these and neither can reach
another tenant's rows. The redemption rows themselves are written by the
database (the trigger and the repair job), never here.

A code is, in this order of precedence:
  reclaimed  taken back by the operator; dead for good
  redeemed   a customer activated it (it has, or is about to get, a redemption)
  expired    not redeemed by its sell-by, or marked expired
  disabled   disabled from the vouchers page rather than reclaimed
  remaining  still sellable
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import and_, case, func, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    ISPOperator,
    Plan,
    Reseller,
    ResellerAccountEntry,
    ResellerAllocation,
    ResellerRedemption,
    ResellerVoucherAllocation,
    Voucher,
)
from src.modules import audit
from src.modules.audit.redact import mask_code
from src.modules.vouchers.engine import generate_unique_codes, generate_voucher_password

COMMISSION_TYPES = ("percent", "fixed")
MAX_ALLOCATION = 1000
MAX_REDEEM_DAYS = 3650
# One cash handover. Generous for a hotspot reseller; stops a typo of three
# extra zeros becoming a balance nobody can explain.
MAX_PAYMENT_GHS = Decimal("100000.00")
NOTE_MAX = 500
_CENT = Decimal("0.01")

STATUSES = ("remaining", "redeemed", "expired", "reclaimed", "disabled")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENT)


def _bad(detail: str, status: int = 400):
    raise HTTPException(status_code=status, detail=detail)


def _note(text: str | None) -> str | None:
    return (text or "").strip()[:NOTE_MAX] or None


def parse_amount(raw, *, field: str = "amount_ghs", maximum: Decimal | None = None, allow_zero: bool = False) -> Decimal:
    """A money amount from a request: finite, two decimal places at most."""
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        _bad(f"{field} must be a number")
    if not value.is_finite() or value != value.quantize(_CENT):
        _bad(f"{field} must be an amount in cedis with at most two decimal places")
    if value < 0 or (value == 0 and not allow_zero):
        _bad(f"{field} must be greater than zero")
    if maximum is not None and value > maximum:
        _bad(f"{field} can't be more than GHS {maximum}")
    return value


def parse_commission(payload: dict) -> tuple[str, Decimal]:
    ctype = payload.get("commission_type")
    if ctype not in COMMISSION_TYPES:
        _bad("commission_type must be 'percent' or 'fixed'")
    value = parse_amount(payload.get("commission_value"), field="commission_value", allow_zero=True)
    if ctype == "percent" and value > 100:
        _bad("A percentage commission can't be more than 100")
    return ctype, value


def commission_view(ctype: str | None, value) -> dict | None:
    return None if ctype is None else {"type": ctype, "value": float(_money(value))}


async def owned_reseller(db: AsyncSession, operator_id: uuid.UUID, reseller_id: uuid.UUID) -> Reseller:
    reseller = (
        await db.execute(select(Reseller).where(Reseller.id == reseller_id, Reseller.isp_operator_id == operator_id))
    ).scalar_one_or_none()
    if reseller is None:
        _bad("Reseller not found", 404)
    return reseller


# ── allocation ────────────────────────────────────────────────────────────


def _redeem_by(raw, default_days: int | None, now: datetime) -> datetime | None:
    """The sell-by: an explicit date-time, or the operator's default days, or
    none. ``raw`` may be None (use the default) or "" (explicitly none)."""
    if raw == "":
        return None
    if raw is None:
        return now + timedelta(days=default_days) if default_days else None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        _bad("redeem_by must be an ISO date-time")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if value <= now:
        _bad("redeem_by must be in the future")
    if value > now + timedelta(days=MAX_REDEEM_DAYS):
        _bad(f"redeem_by can't be more than {MAX_REDEEM_DAYS} days away")
    return value


async def allocate(
    db: AsyncSession, *, operator: ISPOperator, reseller: Reseller, plan_id, quantity, redeem_by, admin_id: uuid.UUID,
) -> tuple[ResellerAllocation, list[Voucher]]:
    """``quantity`` new codes of ``plan_id`` for ``reseller``, priced at the
    plan's price now. The caller commits."""
    if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_ALLOCATION:
        _bad(f"quantity must be a whole number from 1 to {MAX_ALLOCATION}")
    if not reseller.is_active:
        _bad("This reseller is disabled; enable them before allocating codes", 409)
    try:
        plan_uuid = uuid.UUID(str(plan_id))
    except ValueError:
        _bad("Plan not found", 404)
    plan = (
        await db.execute(select(Plan).where(Plan.id == plan_uuid, Plan.isp_operator_id == operator.id))
    ).scalar_one_or_none()
    if plan is None:
        _bad("Plan not found", 404)
    if plan.is_active is not True:
        _bad("This plan is deactivated; codes for it would not work", 409)

    now = _now()
    sell_by = _redeem_by(redeem_by, operator.reseller_redeem_days, now)
    allocation = ResellerAllocation(
        isp_operator_id=operator.id, reseller_id=reseller.id, plan_id=plan.id, quantity=quantity,
        unit_price_ghs=_money(plan.price_ghs), redeem_by=sell_by, created_by_admin_id=admin_id,
    )
    db.add(allocation)
    await db.flush()

    # A code and a username per voucher, from one pool, as generate_vouchers does.
    strings = await generate_unique_codes(db, quantity * 2)
    batch_id = f"RES-{str(allocation.id)[:8]}"
    vouchers = []
    for i in range(quantity):
        voucher = Voucher(
            isp_operator_id=operator.id, plan_id=plan.id, site_id=plan.site_id or reseller.site_id,
            code=strings[2 * i], username=strings[2 * i + 1], password=generate_voucher_password(),
            status="unused", device_policy="single", max_devices=1,
            # The plan's clock starts at first use (Accounting-Start); the
            # sell-by is separate.
            expires_at=None, redeem_by=sell_by, batch_id=batch_id, source="reseller",
        )
        db.add(voucher)
        vouchers.append(voucher)
    await db.flush()
    for voucher in vouchers:
        db.add(ResellerVoucherAllocation(
            reseller_id=reseller.id, voucher_id=voucher.id, purchase_price_ghs=allocation.unit_price_ghs,
            allocation_id=allocation.id, isp_operator_id=operator.id,
        ))
    audit.record(
        db, "reseller.allocate", target_type="reseller", target_id=reseller.id,
        summary=f"Allocated {quantity} {plan.name} code(s) to reseller {reseller.name}",
        detail={"allocation_id": allocation.id, "plan_id": plan.id, "plan_name": plan.name, "quantity": quantity,
                "unit_price_ghs": allocation.unit_price_ghs, "redeem_by": sell_by, "batch_id": batch_id},
    )
    return allocation, vouchers


# ── reclaim ───────────────────────────────────────────────────────────────


async def reclaim(
    db: AsyncSession, *, operator_id: uuid.UUID, reseller: Reseller, voucher_ids: list | None, allocation_id,
    reason: str | None, admin_id: uuid.UUID,
) -> int:
    """Take back unredeemed codes: named ones (all or nothing), or every
    remaining code of one allocation. Each is disabled for good, at once: the
    portal and RADIUS both refuse a disabled voucher. The caller commits."""
    if bool(voucher_ids) == bool(allocation_id):
        _bad("Give either voucher_ids or allocation_id")
    reason = _note(reason)
    if not reason:
        _bad("Give a reason for reclaiming")

    conditions = [
        ResellerVoucherAllocation.reseller_id == reseller.id,
        ResellerVoucherAllocation.isp_operator_id == operator_id,
        ResellerVoucherAllocation.allocation_id.is_not(None),
        ResellerVoucherAllocation.status == "allocated",
        Voucher.isp_operator_id == operator_id,
        Voucher.status == "unused",
        Voucher.activated_at.is_(None),
    ]
    if voucher_ids:
        try:
            wanted = {uuid.UUID(str(v)) for v in voucher_ids}
        except ValueError:
            _bad("voucher_ids must be voucher ids")
        if len(wanted) > MAX_ALLOCATION:
            _bad(f"Reclaim at most {MAX_ALLOCATION} codes at a time")
        conditions.append(Voucher.id.in_(wanted))
    else:
        try:
            conditions.append(ResellerVoucherAllocation.allocation_id == uuid.UUID(str(allocation_id)))
        except ValueError:
            _bad("Allocation not found", 404)

    # Locked, so a customer redeeming one of these right now either finishes
    # first (and it is no longer unused) or waits and finds it disabled.
    rows = (
        await db.execute(
            select(Voucher.id, Voucher.code, ResellerVoucherAllocation.id)
            .join(ResellerVoucherAllocation, ResellerVoucherAllocation.voucher_id == Voucher.id)
            .where(and_(*conditions))
            .with_for_update(of=[Voucher, ResellerVoucherAllocation])
        )
    ).all()
    if voucher_ids and len(rows) != len(wanted):
        _bad("Some of those codes aren't this reseller's unredeemed codes; nothing was reclaimed", 409)
    if not rows:
        return 0

    now = _now()
    ids = [r[0] for r in rows]
    await db.execute(update(Voucher).where(Voucher.id.in_(ids), Voucher.status == "unused").values(status="disabled"))
    await db.execute(
        update(ResellerVoucherAllocation)
        .where(ResellerVoucherAllocation.id.in_([r[2] for r in rows]))
        .values(status="reclaimed", reclaimed_at=now, reclaimed_by_admin_id=admin_id, reclaim_reason=reason)
    )
    audit.record(
        db, "reseller.reclaim", target_type="reseller", target_id=reseller.id,
        summary=f"Reclaimed {len(rows)} unredeemed code(s) from reseller {reseller.name}",
        detail={"count": len(rows), "allocation_id": allocation_id, "reason": reason,
                "code_suffixes": [mask_code(r[1]) for r in rows[:50]]},
    )
    return len(rows)


# ── what each code is, and the totals ─────────────────────────────────────


def _code_state(now: datetime):
    redeemed = or_(ResellerRedemption.id.is_not(None), Voucher.activated_at.is_not(None))
    return case(
        (ResellerVoucherAllocation.status == "reclaimed", literal("reclaimed")),
        (redeemed, literal("redeemed")),
        (or_(Voucher.status == "expired",
             and_(Voucher.status == "unused", Voucher.redeem_by.is_not(None), Voucher.redeem_by <= now)),
         literal("expired")),
        (Voucher.status == "unused", literal("remaining")),
        else_=literal("disabled"),
    )


def _codes_query(operator_id: uuid.UUID, now: datetime):
    """Every allocated (not wallet-era) code of the operator, with its state.
    Callers narrow it to their reseller(s)."""
    state = _code_state(now).label("state")
    return (
        select(Voucher, ResellerVoucherAllocation, ResellerAllocation, ResellerRedemption, Plan.name, state)
        .join(ResellerVoucherAllocation, ResellerVoucherAllocation.voucher_id == Voucher.id)
        .join(ResellerAllocation, ResellerAllocation.id == ResellerVoucherAllocation.allocation_id)
        .join(Plan, Plan.id == ResellerAllocation.plan_id)
        .outerjoin(ResellerRedemption, ResellerRedemption.voucher_id == Voucher.id)
        .where(
            ResellerVoucherAllocation.isp_operator_id == operator_id,
            Voucher.isp_operator_id == operator_id,
            ResellerAllocation.isp_operator_id == operator_id,
        )
    ), state


async def list_codes(
    db: AsyncSession, operator_id: uuid.UUID, reseller_id: uuid.UUID, *, state: str | None = None,
    allocation_id: uuid.UUID | None = None, limit: int = 500,
) -> list[dict]:
    if state is not None and state not in STATUSES:
        _bad(f"status must be one of {', '.join(STATUSES)}")
    now = _now()
    query, state_col = _codes_query(operator_id, now)
    query = query.where(ResellerVoucherAllocation.reseller_id == reseller_id)
    if state:
        query = query.where(state_col == state)
    if allocation_id:
        query = query.where(ResellerAllocation.id == allocation_id)
    rows = (await db.execute(query.order_by(ResellerAllocation.created_at.desc(), Voucher.code).limit(max(1, min(limit, 1000))))).all()
    return [
        {
            "voucher_id": str(v.id), "code": v.code, "status": st, "plan_id": str(a.plan_id), "plan_name": plan_name,
            "allocation_id": str(a.id), "allocated_at": a.created_at.isoformat(), "unit_price_ghs": float(a.unit_price_ghs),
            "redeem_by": v.redeem_by.isoformat() if v.redeem_by else None,
            "redeemed_at": (r.redeemed_at if r else v.activated_at).isoformat() if (r or v.activated_at) else None,
            "commission_ghs": float(r.commission_ghs) if r else None,
            "reclaimed_at": va.reclaimed_at.isoformat() if va.reclaimed_at else None,
        }
        for v, va, a, r, plan_name, st in rows
    ]


def _zero() -> dict:
    return {"allocated": 0, **{s: 0 for s in STATUSES}, "remaining_value_ghs": Decimal("0"),
            "value_sold_ghs": Decimal("0"), "commission_ghs": Decimal("0"), "owed_ghs": Decimal("0"), "repaired": 0}


async def _per_plan(db: AsyncSession, operator_id: uuid.UUID, reseller_ids: list[uuid.UUID]) -> dict:
    """{reseller_id: {plan_id: counts and money}} from one grouped query."""
    now = _now()
    query, state = _codes_query(operator_id, now)
    query = query.with_only_columns(
        ResellerVoucherAllocation.reseller_id, ResellerAllocation.plan_id, Plan.name, state,
        func.count(), func.coalesce(func.sum(ResellerAllocation.unit_price_ghs), 0),
        func.coalesce(func.sum(ResellerRedemption.price_ghs), 0),
        func.coalesce(func.sum(ResellerRedemption.commission_ghs), 0),
        func.coalesce(func.sum(ResellerRedemption.owed_ghs), 0),
        func.count().filter(ResellerRedemption.source == "repair"),
    ).where(ResellerVoucherAllocation.reseller_id.in_(reseller_ids)).group_by(
        ResellerVoucherAllocation.reseller_id, ResellerAllocation.plan_id, Plan.name, state
    )
    out: dict = {}
    for rid, plan_id, plan_name, st, n, at_price, sold, commission, owed, repaired in (await db.execute(query)).all():
        row = out.setdefault(rid, {}).setdefault(plan_id, {"plan_id": str(plan_id), "plan_name": plan_name, **_zero()})
        row["allocated"] += n
        row[st] += n
        if st == "remaining":
            row["remaining_value_ghs"] += _money(at_price)
        row["value_sold_ghs"] += _money(sold)
        row["commission_ghs"] += _money(commission)
        row["owed_ghs"] += _money(owed)
        row["repaired"] += repaired
    return out


async def _paid(db: AsyncSession, operator_id: uuid.UUID, reseller_ids: list[uuid.UUID]) -> dict:
    signed = case((ResellerAccountEntry.kind == "payment", ResellerAccountEntry.amount_ghs),
                  else_=-ResellerAccountEntry.amount_ghs)
    rows = (
        await db.execute(
            select(ResellerAccountEntry.reseller_id, func.coalesce(func.sum(signed), 0))
            .where(ResellerAccountEntry.isp_operator_id == operator_id, ResellerAccountEntry.reseller_id.in_(reseller_ids))
            .group_by(ResellerAccountEntry.reseller_id)
        )
    ).all()
    return {rid: _money(total) for rid, total in rows}


def balance_view(owed: Decimal, paid: Decimal) -> dict:
    """Outstanding is never shown negative: an overpayment is credit, carried
    against future sales."""
    net = _money(owed) - _money(paid)
    return {"paid_ghs": float(_money(paid)), "outstanding_ghs": float(max(net, Decimal("0"))),
            "credit_ghs": float(max(-net, Decimal("0")))}


def _floats(row: dict) -> dict:
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}


async def summaries(db: AsyncSession, operator_id: uuid.UUID, reseller_ids: list[uuid.UUID]) -> dict:
    """{reseller_id: {"totals": ..., "plans": [...]}} for each id."""
    per_plan = await _per_plan(db, operator_id, reseller_ids)
    paid = await _paid(db, operator_id, reseller_ids)
    out = {}
    for rid in reseller_ids:
        plans = list(per_plan.get(rid, {}).values())
        totals = _zero()
        for row in plans:
            for key, value in row.items():
                if key in totals:
                    totals[key] += value
        balance = balance_view(totals["owed_ghs"], paid.get(rid, Decimal("0")))
        out[rid] = {"totals": {**_floats(totals), **balance},
                    "plans": sorted((_floats(p) for p in plans), key=lambda p: p["plan_name"])}
    return out


async def summary(db: AsyncSession, operator_id: uuid.UUID, reseller_id: uuid.UUID) -> dict:
    return (await summaries(db, operator_id, [reseller_id]))[reseller_id]


async def list_allocations(db: AsyncSession, operator_id: uuid.UUID, reseller_id: uuid.UUID) -> list[dict]:
    rows = (
        await db.execute(
            select(ResellerAllocation, Plan.name)
            .join(Plan, Plan.id == ResellerAllocation.plan_id)
            .where(ResellerAllocation.reseller_id == reseller_id, ResellerAllocation.isp_operator_id == operator_id)
            .order_by(ResellerAllocation.created_at.desc())
        )
    ).all()
    return [
        {"id": str(a.id), "plan_id": str(a.plan_id), "plan_name": name, "quantity": a.quantity,
         "unit_price_ghs": float(a.unit_price_ghs), "redeem_by": a.redeem_by.isoformat() if a.redeem_by else None,
         "created_at": a.created_at.isoformat()}
        for a, name in rows
    ]


# ── the account ───────────────────────────────────────────────────────────


async def account(db: AsyncSession, operator_id: uuid.UUID, reseller_id: uuid.UUID, *, include_recorder: bool) -> dict:
    """The running account, oldest first: redemptions grouped by day (repaired
    ones counted apart), then every payment and reversal, each line with the
    balance after it. Positive balance: the reseller owes; negative: credit."""
    day = func.date_trunc("day", ResellerRedemption.redeemed_at).label("day")
    days = (
        await db.execute(
            select(day, func.count(), func.sum(ResellerRedemption.price_ghs), func.sum(ResellerRedemption.commission_ghs),
                   func.sum(ResellerRedemption.owed_ghs), func.count().filter(ResellerRedemption.source == "repair"),
                   func.max(ResellerRedemption.redeemed_at))
            .where(ResellerRedemption.reseller_id == reseller_id, ResellerRedemption.isp_operator_id == operator_id)
            .group_by(day)
        )
    ).all()
    entries = (
        await db.execute(
            select(ResellerAccountEntry)
            .where(ResellerAccountEntry.reseller_id == reseller_id, ResellerAccountEntry.isp_operator_id == operator_id)
        )
    ).scalars().all()
    reversed_ids = {e.reverses_entry_id for e in entries if e.reverses_entry_id}

    lines = []
    for d, n, value, commission, owed, repaired, last in days:
        lines.append((last, 0, {
            "type": "redemptions", "date": d.date().isoformat(), "count": n, "value_ghs": _money(value),
            "commission_ghs": _money(commission), "amount_ghs": _money(owed), "repaired": repaired,
        }))
    for e in entries:
        line = {"type": e.kind, "id": str(e.id), "date": e.received_on.isoformat(), "amount_ghs": -_money(e.amount_ghs)
                if e.kind == "payment" else _money(e.amount_ghs), "note": e.note, "recorded_at": e.created_at.isoformat(),
                "reverses_entry_id": str(e.reverses_entry_id) if e.reverses_entry_id else None,
                "reversed": e.id in reversed_ids}
        if include_recorder:
            line["recorded_by_admin_id"] = str(e.recorded_by_admin_id)
        lines.append((e.created_at, 1, line))
    lines.sort(key=lambda item: (item[0], item[1]))

    balance = Decimal("0")
    out = []
    for _, _, line in lines:
        balance += line["amount_ghs"]
        out.append(_floats({**line, "balance_ghs": balance}))
    return {"lines": out, "balance_ghs": float(balance)}


async def record_payment(
    db: AsyncSession, *, operator_id: uuid.UUID, reseller: Reseller, amount, received_on, note, admin_id: uuid.UUID,
) -> ResellerAccountEntry:
    """Cash the reseller handed over. The caller commits."""
    amount = parse_amount(amount, maximum=MAX_PAYMENT_GHS)
    today = _now().date()
    if received_on in (None, ""):
        received = today
    else:
        try:
            received = date.fromisoformat(str(received_on))
        except ValueError:
            _bad("received_on must be a date (YYYY-MM-DD)")
    if received > today:
        _bad("received_on can't be in the future")
    entry = ResellerAccountEntry(
        isp_operator_id=operator_id, reseller_id=reseller.id, kind="payment", amount_ghs=amount,
        received_on=received, note=_note(note), recorded_by_admin_id=admin_id,
    )
    db.add(entry)
    await db.flush()
    audit.record(
        db, "reseller.payment_recorded", target_type="reseller", target_id=reseller.id,
        summary=f"Recorded a payment of GHS {amount} from reseller {reseller.name}",
        detail={"entry_id": entry.id, "amount_ghs": amount, "received_on": received, "note": entry.note},
    )
    return entry


async def reverse_payment(
    db: AsyncSession, *, operator_id: uuid.UUID, reseller: Reseller, entry_id: uuid.UUID, reason, admin_id: uuid.UUID,
) -> ResellerAccountEntry:
    """Undo a recorded payment, in full, once. The caller commits."""
    reason = _note(reason)
    if not reason:
        _bad("Give a reason for the reversal")
    target = (
        await db.execute(
            select(ResellerAccountEntry)
            .where(ResellerAccountEntry.id == entry_id, ResellerAccountEntry.reseller_id == reseller.id,
                   ResellerAccountEntry.isp_operator_id == operator_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if target is None:
        _bad("Payment not found", 404)
    if target.kind != "payment":
        _bad("A reversal can't be reversed; record a new payment instead", 409)
    already = (
        await db.execute(select(ResellerAccountEntry.id).where(ResellerAccountEntry.reverses_entry_id == target.id))
    ).scalar_one_or_none()
    if already is not None:
        _bad("This payment has already been reversed", 409)
    entry = ResellerAccountEntry(
        isp_operator_id=operator_id, reseller_id=reseller.id, kind="reversal", amount_ghs=target.amount_ghs,
        reverses_entry_id=target.id, received_on=_now().date(), note=reason, recorded_by_admin_id=admin_id,
    )
    db.add(entry)
    try:
        await db.flush()
    except IntegrityError:
        # Another reversal of the same payment committed first (UNIQUE).
        await db.rollback()
        _bad("This payment has already been reversed", 409)
    audit.record(
        db, "reseller.payment_reversed", target_type="reseller", target_id=reseller.id,
        summary=f"Reversed a payment of GHS {target.amount_ghs} from reseller {reseller.name}",
        detail={"entry_id": entry.id, "reverses_entry_id": target.id, "amount_ghs": target.amount_ghs,
                "received_on": target.received_on, "reason": reason},
    )
    return entry
