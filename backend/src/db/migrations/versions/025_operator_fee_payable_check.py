"""Constrain monthly_fee_ghs to values that can actually be billed.

A fee is either 0 — the operator is exempt and never invoiced — or at least
Paystack's GHS 1.00 minimum charge. Anything between produces an invoice that
can never be paid: it runs through the grace period and `enforce_billing`
suspends the operator, blocking their captive portal. That is the Phase 0
incident in a different guise.

The billing jobs already skip such operators and the API rejects such values,
but a CHECK makes the state unreachable rather than merely avoided — the same
discipline as provider_catalog's availability and rate constraints. A stray
UPDATE at the psql prompt cannot recreate the trap.

Safe to apply: no row is outside the permitted range.
"""
from alembic import op

revision = "025_operator_fee_payable_check"
down_revision = "024_webhook_hardening"
branch_labels = None
depends_on = None

# Mirrors billing.service.PAYSTACK_MINIMUM_GHS.
CONSTRAINT = "ck_isp_operators_fee_zero_or_payable"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE isp_operators ADD CONSTRAINT {CONSTRAINT} "
        "CHECK (monthly_fee_ghs = 0 OR monthly_fee_ghs >= 1.00)"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE isp_operators DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
