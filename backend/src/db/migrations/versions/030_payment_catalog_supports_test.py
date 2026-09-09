"""Re-sync the provider catalog so the payment entries (Paystack, Flutterwave)
carry ``credential_schema.supports_test = true``.

Both payment providers already have a working ``verify_credentials()`` (a
read-only GET against the provider) wired into the shared credential ``/test``
endpoint since the payment build. Phase 7 of the SMS provider-selection work made
the operator credentials UI render the "Test connection" button from this flag
rather than assuming every provider has one — so the flag now has to be present
on the payment rows too, matching how migration 029 added it for Africa's
Talking.

``sync_provider_catalog`` is an idempotent upsert of every catalog entry from
``src/modules/platform/provider_catalog.py``; it never writes ``is_available`` or
``platform_rate_per_message``.

Reversible: downgrade strips ``supports_test`` back out of the payment schemas.
"""
from alembic import op

revision = "030_payment_catalog_supports_test"
down_revision = "029_sms_catalog_supports_test"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.modules.platform.provider_catalog import sync_provider_catalog

    sync_provider_catalog(op.get_bind())


def downgrade() -> None:
    op.execute(
        """
        UPDATE provider_catalog
        SET credential_schema = credential_schema - 'supports_test',
            updated_at = NOW()
        WHERE category = 'payment'
          AND credential_schema ? 'supports_test'
        """
    )
