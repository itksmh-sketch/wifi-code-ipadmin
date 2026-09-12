"""Add 'arkesel_platform' to operator_sms_provider, and a uniqueness
constraint on sms_usage_records.provider_reference.

'arkesel_platform' is the operator-facing opt-in marker for the
platform-provided SMS gateway — an operator_sms_credentials row with this
provider value carries no real secrets (an empty placeholder
credentials_encrypted blob), and exists purely so the existing
uq_operator_sms_credentials_one_active partial unique index can guarantee at
most one active SMS provider per operator, platform-gateway selection
included, for free. See sms.provider_resolver and sms.credentials_routes'
/activate-platform endpoint.

The provider_reference uniqueness makes sms.metering.record_platform_sms_usage
idempotent: a bounded retry after a commit whose acknowledgment was lost can
safely re-attempt the same insert (ON CONFLICT DO NOTHING) instead of
double-billing. A plain (non-partial) unique constraint is correct here —
Postgres already treats multiple NULLs as non-conflicting under a standard
UNIQUE constraint, so the rare send with no provider_reference is unaffected.

Same ADD VALUE pattern as migration 031 (PG 12+ allows it inside a
transaction as long as the new value isn't used in the same transaction,
which it isn't here).
"""
from alembic import op

revision = "035_arkesel_platform_provider_and_usage_unique"
down_revision = "034_platform_sms_rate_and_arkesel_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE operator_sms_provider ADD VALUE IF NOT EXISTS 'arkesel_platform'")
    op.create_unique_constraint(
        "uq_sms_usage_records_provider_reference", "sms_usage_records", ["provider_reference"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_sms_usage_records_provider_reference", "sms_usage_records", type_="unique")
    # Postgres cannot drop an enum value.
