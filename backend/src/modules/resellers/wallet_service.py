import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional, Sequence

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    CommissionRule,
    Plan,
    Reseller,
    ResellerWallet,
    ResellerWalletTransaction,
    ResellerVoucherAllocation,
    Voucher,
)


class InsufficientFundsError(Exception):
    pass


@dataclass(frozen=True)
class _RuleView:
    id: uuid.UUID
    reseller_id: Optional[uuid.UUID]
    plan_id: Optional[uuid.UUID]
    type: str
    value: Decimal
    is_active: bool


def _rule_specificity_rank(rule_reseller_id: Optional[uuid.UUID], rule_plan_id: Optional[uuid.UUID], reseller_id: uuid.UUID, plan_id: uuid.UUID) -> int:
    """
    Lower rank = higher priority.
    Priority order:
      0: reseller_id=this reseller AND plan_id=this plan
      1: reseller_id=this reseller AND plan_id IS NULL
      2: reseller_id IS NULL AND plan_id=this plan
      3: reseller_id IS NULL AND plan_id IS NULL
    """
    reseller_specific = rule_reseller_id == reseller_id
    plan_specific = rule_plan_id == plan_id

    if reseller_specific and plan_specific:
        return 0
    if reseller_specific and rule_plan_id is None:
        return 1
    if rule_reseller_id is None and plan_specific:
        return 2
    if rule_reseller_id is None and rule_plan_id is None:
        return 3
    return 999


def _pick_commission_rule(
    rules: Iterable[_RuleView],
    *,
    reseller_id: uuid.UUID,
    plan_id: uuid.UUID,
) -> Optional[_RuleView]:
    applicable: list[_RuleView] = []
    for r in rules:
        if not r.is_active:
            continue
        if r.reseller_id not in (None, reseller_id):
            continue
        if r.plan_id not in (None, plan_id):
            continue
        if _rule_specificity_rank(r.reseller_id, r.plan_id, reseller_id, plan_id) == 999:
            continue
        applicable.append(r)

    if not applicable:
        return None

    # Deterministic tiebreakers: higher specificity, then id.
    applicable.sort(
        key=lambda r: (
            _rule_specificity_rank(r.reseller_id, r.plan_id, reseller_id, plan_id),
            str(r.id),
        )
    )
    return applicable[0]


def _money_2dp(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class WalletService:
    async def _get_reseller_operator_id(self, db: AsyncSession, reseller_id: uuid.UUID) -> uuid.UUID:
        operator_id = (
            await db.execute(select(Reseller.isp_operator_id).where(Reseller.id == reseller_id, Reseller.is_active == True))
        ).scalar_one_or_none()
        if operator_id is None:
            raise ValueError("Reseller not found")
        return operator_id

    async def _get_or_create_wallet_for_update(self, db: AsyncSession, reseller_id: uuid.UUID) -> ResellerWallet:
        res = await db.execute(
            select(ResellerWallet).where(ResellerWallet.reseller_id == reseller_id).with_for_update()
        )
        wallet = res.scalar_one_or_none()
        if wallet:
            return wallet

        wallet = ResellerWallet(reseller_id=reseller_id, balance_ghs=Decimal("0.00"))
        db.add(wallet)
        await db.flush()
        # Re-lock row to enforce consistent behavior.
        res2 = await db.execute(select(ResellerWallet).where(ResellerWallet.id == wallet.id).with_for_update())
        return res2.scalar_one()

    async def calculate_commission(self, db: AsyncSession, reseller_id: uuid.UUID, plan_id: uuid.UUID) -> Decimal:
        operator_id = await self._get_reseller_operator_id(db, reseller_id)
        plan_res = await db.execute(select(Plan).where(Plan.id == plan_id, Plan.isp_operator_id == operator_id))
        plan = plan_res.scalar_one_or_none()
        if not plan:
            raise ValueError("Plan not found")

        rules_res = await db.execute(
            select(CommissionRule).where(
                CommissionRule.is_active == True,  # noqa: E712
                CommissionRule.isp_operator_id == operator_id,
                or_(CommissionRule.reseller_id == reseller_id, CommissionRule.reseller_id.is_(None)),
                or_(CommissionRule.plan_id == plan_id, CommissionRule.plan_id.is_(None)),
            )
        )
        rules = [
            _RuleView(
                id=r.id,
                reseller_id=r.reseller_id,
                plan_id=r.plan_id,
                type=r.type,
                value=Decimal(str(r.value)),
                is_active=bool(r.is_active),
            )
            for r in rules_res.scalars().all()
        ]

        picked = _pick_commission_rule(rules, reseller_id=reseller_id, plan_id=plan_id)
        if not picked:
            return Decimal("0.00")

        if picked.type == "flat":
            return _money_2dp(picked.value)
        if picked.type == "percentage":
            pct = picked.value / Decimal("100")
            return _money_2dp(Decimal(str(plan.price_ghs)) * pct)

        raise ValueError("Invalid commission rule type")

    async def get_balance(self, db: AsyncSession, reseller_id: uuid.UUID) -> Decimal:
        res = await db.execute(select(ResellerWallet.balance_ghs).where(ResellerWallet.reseller_id == reseller_id))
        bal = res.scalar_one_or_none()
        if bal is None:
            return Decimal("0.00")
        return _money_2dp(Decimal(str(bal)))

    async def get_transactions(
        self,
        db: AsyncSession,
        reseller_id: uuid.UUID,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> Sequence[ResellerWalletTransaction]:
        res = await db.execute(select(ResellerWallet.id).where(ResellerWallet.reseller_id == reseller_id))
        wallet_id = res.scalar_one_or_none()
        if not wallet_id:
            return []

        q = (
            select(ResellerWalletTransaction)
            .where(ResellerWalletTransaction.wallet_id == wallet_id)
            .order_by(desc(ResellerWalletTransaction.created_at))
            .limit(limit)
            .offset(offset)
        )
        r2 = await db.execute(q)
        return list(r2.scalars().all())

    async def topup(
        self,
        db: AsyncSession,
        *,
        reseller_id: uuid.UUID,
        amount_ghs: Decimal,
        description: str,
        triggered_by: str = "admin",
        reference: Optional[str] = None,
    ) -> ResellerWalletTransaction:
        if amount_ghs <= 0:
            raise ValueError("Topup amount must be positive")

        wallet = await self._get_or_create_wallet_for_update(db, reseller_id)
        delta = _money_2dp(amount_ghs)
        new_balance = _money_2dp(Decimal(str(wallet.balance_ghs)) + delta)
        wallet.balance_ghs = new_balance
        wallet.lifetime_topped_up_ghs = _money_2dp(Decimal(str(wallet.lifetime_topped_up_ghs)) + delta)
        wallet.updated_at = func.now()

        tx = ResellerWalletTransaction(
            wallet_id=wallet.id,
            type="topup",
            amount_ghs=delta,
            balance_after_ghs=new_balance,
            description=description,
            reference=reference or str(uuid.uuid4()),
            voucher_id=None,
            triggered_by=triggered_by,
        )
        db.add(tx)
        await db.flush()
        return tx

    async def purchase(
        self,
        db: AsyncSession,
        *,
        reseller_id: uuid.UUID,
        voucher_id: uuid.UUID,
        plan_id: uuid.UUID,
        triggered_by: str = "reseller",
        description: Optional[str] = None,
        reference: Optional[str] = None,
    ) -> tuple[ResellerWalletTransaction, ResellerVoucherAllocation]:
        operator_id = await self._get_reseller_operator_id(db, reseller_id)
        plan_res = await db.execute(select(Plan).where(Plan.id == plan_id, Plan.isp_operator_id == operator_id))
        plan = plan_res.scalar_one_or_none()
        if not plan:
            raise ValueError("Plan not found")

        voucher_res = await db.execute(select(Voucher).where(Voucher.id == voucher_id, Voucher.isp_operator_id == operator_id))
        voucher = voucher_res.scalar_one_or_none()
        if not voucher:
            raise ValueError("Voucher not found")
        if voucher.plan_id != plan_id:
            raise ValueError("Voucher plan mismatch")

        wallet = await self._get_or_create_wallet_for_update(db, reseller_id)
        commission = await self.calculate_commission(db, reseller_id, plan_id)
        unit_cost = _money_2dp(Decimal(str(plan.price_ghs)) - commission)
        if unit_cost < 0:
            unit_cost = Decimal("0.00")

        current = _money_2dp(Decimal(str(wallet.balance_ghs)))
        new_balance = _money_2dp(current - unit_cost)
        if new_balance < Decimal("0.00"):
            raise InsufficientFundsError("Insufficient wallet balance")

        wallet.balance_ghs = new_balance
        wallet.lifetime_spent_ghs = _money_2dp(Decimal(str(wallet.lifetime_spent_ghs)) + unit_cost)
        wallet.updated_at = func.now()

        tx = ResellerWalletTransaction(
            wallet_id=wallet.id,
            type="purchase",
            amount_ghs=_money_2dp(Decimal("0.00") - unit_cost),
            balance_after_ghs=new_balance,
            description=description or f"Voucher purchase: plan={plan.name}",
            reference=reference or str(uuid.uuid4()),
            voucher_id=voucher_id,
            triggered_by=triggered_by,
        )
        db.add(tx)

        alloc = ResellerVoucherAllocation(
            reseller_id=reseller_id,
            voucher_id=voucher_id,
            purchase_price_ghs=unit_cost,
        )
        db.add(alloc)
        await db.flush()
        return tx, alloc
