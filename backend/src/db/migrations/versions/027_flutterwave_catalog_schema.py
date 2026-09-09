"""Re-sync the provider catalog so the Flutterwave payment entry carries its real
credential schema and is marked integrated.

Step 3 of the payment multi-provider build shipped `FlutterwaveProvider`. The
catalog row for it still says "Not yet integrated" with an empty field list —
this brings it in line with `src/modules/platform/provider_catalog.py`.

`sync_provider_catalog` is an idempotent upsert of every catalog entry. It
deliberately never writes `is_available` or `platform_rate_per_message` — those
belong to the platform admin. So Flutterwave stays **unavailable** after this
migration; the admin flips it on `/platform/providers` only after the live
sandbox round-trip.
"""
from alembic import op

revision = "027_flutterwave_catalog_schema"
down_revision = "026_operator_payment_credentials_jsonb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.modules.platform.provider_catalog import sync_provider_catalog

    sync_provider_catalog(op.get_bind())


def downgrade() -> None:
    op.execute(
        """
        UPDATE provider_catalog
        SET is_integrated = false,
            credential_schema = '{"configured_by": "operator", "fields": []}'::jsonb,
            description = 'Not yet integrated.',
            updated_at = NOW()
        WHERE category = 'payment' AND provider_key = 'flutterwave'
        """
    )
