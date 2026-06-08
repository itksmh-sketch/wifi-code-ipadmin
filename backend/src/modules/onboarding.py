"""
Onboarding checklist helpers.
Each function is called from the relevant service action to mark a step complete.
"""
from __future__ import annotations
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import ISPOperator

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
    current[key] = True
    operator.onboarding_checklist = current
    await db.flush()


async def get_checklist(db: AsyncSession, operator_id: uuid.UUID) -> dict:
    operator = (
        await db.execute(select(ISPOperator).where(ISPOperator.id == operator_id))
    ).scalar_one_or_none()
    base = {k: False for k in CHECKLIST_KEYS}
    if operator and operator.onboarding_checklist:
        base.update({k: bool(v) for k, v in operator.onboarding_checklist.items() if k in CHECKLIST_KEYS})
    return base
