"""Flip arkesel_platform's is_integrated to True.

The send path (resolver branch, registry branch, metering write with
retry/idempotency, billing rollup, reconciliation backstop) is now built and
deployed. is_integrated reflects "the code exists" — the same meaning it
already has for hubtel/africastalking/arkesel (bring-your-own) and
flutterwave, all is_integrated=True/is_available=False right now too.
is_available is the separate, deliberate platform-admin gate, only flipped
after the real live-sandbox transaction described in
docs/deferred-provider-launch-gates.md — unaffected by this migration.

Pure catalog-content sync, same precedent as migration 031: re-running
sync_provider_catalog picks up the changed Python source.
"""
from alembic import op

revision = "036_arkesel_platform_is_integrated"
down_revision = "035_arkesel_platform_provider_and_usage_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.modules.platform.provider_catalog import sync_provider_catalog

    sync_provider_catalog(op.get_bind())


def downgrade() -> None:
    # Catalog content isn't reverted by downgrade -- same precedent as 031/034.
    pass
