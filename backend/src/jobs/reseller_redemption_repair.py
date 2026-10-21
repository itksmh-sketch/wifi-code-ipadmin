"""Job: reseller_redemption_repair — nightly, adds the reseller redemption rows
the database trigger failed to write.

The trigger (vouchers_reseller_redemption, migration 060) fails open: if
recording the redemption errors, the activation it rides on still commits, so
a customer is never refused because of the reseller's account. This finds
every allocated reseller code that has been activated but has no redemption
row and records it through the same reseller_redemption_insert() the trigger
uses, with source 'repair' and the voucher's activation time. The commission is
the rate in force now, which is the only rate there is to use; the ledger marks
these rows so an operator can tell.

Each repaired row is audited in its own transaction with the row itself.
"""
from __future__ import annotations

import structlog
from sqlalchemy import text

from src.db.base import async_session_factory
from src.jobs.job_lock import job_lock
from src.modules import audit
from src.modules.audit.redact import mask_code

logger = structlog.get_logger(__name__)

_LOCK_KEY = "lock:reseller_redemption_repair"
_LOCK_TTL_SECONDS = 1800

_MISSING = text(
    """
    SELECT v.id, v.isp_operator_id, v.code, v.activated_at, va.reseller_id
      FROM vouchers v
      JOIN reseller_voucher_allocations va
        ON va.voucher_id = v.id AND va.allocation_id IS NOT NULL AND va.status = 'allocated'
     WHERE v.activated_at IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM reseller_redemptions rr WHERE rr.voucher_id = v.id)
     ORDER BY v.activated_at
    """
)
_INSERT = text("SELECT reseller_redemption_insert(:voucher_id, :redeemed_at, 'repair')")


async def repair_reseller_redemptions(ctx=None) -> dict:
    async with job_lock((ctx or {}).get("redis"), _LOCK_KEY, _LOCK_TTL_SECONDS) as acquired:
        if not acquired:
            logger.info("reseller_redemption_repair_skipped_overlap", module=__name__)
            return {"skipped": "previous run still in flight"}
        return await _repair()


async def _repair() -> dict:
    stats = {"missing": 0, "repaired": 0, "errors": 0}
    async with async_session_factory() as db:
        missing = (await db.execute(_MISSING)).all()
    stats["missing"] = len(missing)
    for voucher_id, operator_id, code, activated_at, reseller_id in missing:
        try:
            async with async_session_factory() as db:
                inserted = (await db.execute(_INSERT, {"voucher_id": voucher_id, "redeemed_at": activated_at})).scalar()
                if inserted:
                    audit.record_system(
                        db, "reseller.redemption_repaired", isp_operator_id=operator_id,
                        target_type="voucher", target_id=voucher_id,
                        summary=f"Recorded the missing reseller redemption of voucher {mask_code(code)}",
                        detail={"code_suffix": mask_code(code), "reseller_id": reseller_id,
                                "activated_at": activated_at},
                    )
                    stats["repaired"] += 1
                await db.commit()
        except Exception as exc:  # one bad row must not strand the rest until tomorrow
            stats["errors"] += 1
            logger.warning("reseller_redemption_repair_failed", module=__name__, voucher_id=str(voucher_id), error=str(exc))
    level = logger.warning if stats["repaired"] or stats["errors"] else logger.info
    level("reseller_redemption_repair_done", module=__name__, **stats)
    return stats
