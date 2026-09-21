"""Backfill onboarding checklists lost to the in-place-mutation bug.

modules.onboarding.mark_checklist mutated the loaded JSONB dict and assigned
the same object back; with no MutableDict tracking, SQLAlchemy saw no change
and every mark after an operator's first was silently dropped (fixed in the
same change as this migration). Each operator therefore has at most one step
recorded, whatever they have actually done.

This re-derives each step from the evidence that step stands for:
  town_added          a towns row
  router_added        a routers row
  payment_configured  an ACTIVE payment credential (what the save path marks)
  voucher_generated   a source='manual' voucher (operator-generated stock)
  first_sale_made     a successful, non-diagnostic payment
portal_tested is deliberately not touched: whether to keep it, and what should
trigger it, is a separate decision.

Additive only. `||` merges into the stored object and jsonb_strip_nulls drops
every step without evidence, so a step already true is never cleared and a step
is never written false.

Revision ID: 051_backfill_onboarding_checklist
Revises: 050_voucher_source_and_printed_at
"""
from alembic import op

revision = "051_backfill_onboarding_checklist"
down_revision = "050_voucher_source_and_printed_at"
branch_labels = None
depends_on = None

BACKFILL = """
UPDATE isp_operators o
SET onboarding_checklist = COALESCE(o.onboarding_checklist, '{}'::jsonb) || jsonb_strip_nulls(jsonb_build_object(
    'town_added',         CASE WHEN EXISTS (SELECT 1 FROM towns t WHERE t.isp_operator_id = o.id) THEN true END,
    'router_added',       CASE WHEN EXISTS (SELECT 1 FROM routers r WHERE r.isp_operator_id = o.id) THEN true END,
    'payment_configured', CASE WHEN EXISTS (SELECT 1 FROM operator_payment_credentials c
                                            WHERE c.isp_operator_id = o.id AND c.is_active) THEN true END,
    'voucher_generated',  CASE WHEN EXISTS (SELECT 1 FROM vouchers v
                                            WHERE v.isp_operator_id = o.id AND v.source = 'manual') THEN true END,
    'first_sale_made',    CASE WHEN EXISTS (SELECT 1 FROM payment_transactions p
                                            WHERE p.isp_operator_id = o.id AND p.status = 'success'
                                              AND NOT p.is_diagnostic) THEN true END
))
"""


def upgrade():
    op.execute(BACKFILL)


def downgrade():
    # Not reversible: after upgrade there is no record of which steps were set
    # by this backfill and which by the (now working) application. The values
    # it writes are true statements about the operator, so leaving them is safe.
    pass
