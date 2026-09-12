"""Rename provider_catalog.platform_rate_per_message to
platform_rate_per_segment, and add the arkesel_platform catalog row.

The rename reflects a design decision made after migration 021: the platform-
provided SMS gateway bills per SMS segment (computed locally via GSM-7/UCS-2
rules, never trusted from the provider's response), not a flat rate per
message — segment counting is cheap and deterministic, and Arkesel's own
pricing is itself per-segment, so a flat per-message platform rate would
either overcharge short messages or undercharge long ones relative to what
the platform actually pays. The CHECK constraint
(ck_provider_catalog_rate_requires_platform_provided) follows the rename
automatically — Postgres updates a constraint's definition when the column it
references is renamed.

arkesel_platform is the platform-provided SMS gateway actually being built.
The existing africastalking_platform row (migration 021) is earlier, unrelated
scaffolding for a different provider that was never built out — it is left
exactly as it is, not touched by this migration.
"""
from alembic import op

revision = "034_platform_sms_rate_and_arkesel_catalog"
down_revision = "033_platform_sms_credentials_and_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "provider_catalog", "platform_rate_per_message", new_column_name="platform_rate_per_segment"
    )

    from src.modules.platform.provider_catalog import sync_provider_catalog

    sync_provider_catalog(op.get_bind())


def downgrade() -> None:
    op.alter_column(
        "provider_catalog", "platform_rate_per_segment", new_column_name="platform_rate_per_message"
    )
    # arkesel_platform row is left in place — same precedent as migration 031
    # (catalog additions aren't reverted, only the column rename is).
