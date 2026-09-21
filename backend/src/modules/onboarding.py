"""
Onboarding checklist helpers.
Each function is called from the relevant service action to mark a step complete.
"""
from __future__ import annotations
import uuid
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import ISPOperator, PaymentTransaction, Session, Voucher

CHECKLIST_KEYS = [
    "town_added",
    "router_added",
    "payment_configured",
    "portal_tested",
    "voucher_generated",
    "first_sale_made",
]


async def mark_checklist(db: AsyncSession, operator_id: uuid.UUID, key: str) -> None:
    """Idempotently mark a checklist item complete."""
    if key not in CHECKLIST_KEYS:
        return
    operator = (
        await db.execute(select(ISPOperator).where(ISPOperator.id == operator_id))
    ).scalar_one_or_none()
    if not operator:
        return
    current = operator.onboarding_checklist or {}
    if current.get(key):
        return
    # Assign a NEW dict. onboarding_checklist is plain JSONB (no MutableDict),
    # so SQLAlchemy only notices a change when the attribute is given a
    # different object. Mutating the loaded dict and assigning it back is the
    # same object: no UPDATE is emitted and the mark is silently lost. That is
    # exactly what happened for every step after an operator's first — the
    # first only worked because the server default '{}' is falsy, so
    # `or {}` happened to build a fresh dict.
    operator.onboarding_checklist = {**current, key: True}
    await db.flush()


async def get_checklist(db: AsyncSession, operator_id: uuid.UUID) -> dict:
    """Stored marks, plus two steps derived from evidence at read time.

    portal_tested and first_sale_made both hinge on a customer actually getting
    online, and no application code runs at that moment: FreeRADIUS writes the
    sessions row itself via sql.conf. So instead of a mark that nothing could
    set, these read the evidence directly. A stored mark still counts — derived
    evidence only ever adds, never clears.
    """
    operator = (
        await db.execute(select(ISPOperator).where(ISPOperator.id == operator_id))
    ).scalar_one_or_none()
    base = {k: False for k in CHECKLIST_KEYS}
    if operator and operator.onboarding_checklist:
        base.update({k: bool(v) for k, v in operator.onboarding_checklist.items() if k in CHECKLIST_KEYS})
    if operator is None:
        return base

    if not base["portal_tested"]:
        # Any session means the whole chain worked end to end: captive portal,
        # router, RADIUS, and a customer online.
        base["portal_tested"] = bool(
            await db.scalar(select(exists().where(Session.isp_operator_id == operator_id)))
        )

    if not base["first_sale_made"]:
        # A sale is either a real online payment or the first use of a voucher
        # the operator generated and sold by hand — otherwise an operator who
        # only sells printed vouchers could never complete this step. Diagnostic
        # payments are test runs, not sales; reseller vouchers are the
        # reseller's sale, not the operator's.
        online_sale = exists().where(
            PaymentTransaction.isp_operator_id == operator_id,
            PaymentTransaction.status == "success",
            PaymentTransaction.is_diagnostic.is_(False),
        )
        printed_sale = (
            exists()
            .where(Session.isp_operator_id == operator_id, Session.voucher_id == Voucher.id)
            .where(Voucher.isp_operator_id == operator_id, Voucher.source == "manual")
        )
        base["first_sale_made"] = bool(await db.scalar(select(online_sale | printed_sale)))
    return base
