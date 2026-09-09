"""Re-sync the provider catalog so the Africa's Talking SMS entry advertises a
"test connection" capability.

Phase 5 of the SMS provider-selection build gave ``AfricasTalkingSMSProvider`` a
real ``verify_credentials()`` (GET /version1/user — no cost, no SMS). The catalog
row now carries ``credential_schema.supports_test = true`` so the operator config
UI shows the Test-connection button for Africa's Talking and hides it for Hubtel
(which has no documented no-cost check).

``sync_provider_catalog`` is an idempotent upsert of every catalog entry from
``src/modules/platform/provider_catalog.py``. It never writes ``is_available`` or
``platform_rate_per_message`` — those belong to the platform admin — so the SMS
providers stay **unavailable** after this migration.

Reversible: downgrade strips the ``supports_test`` key back out of the SMS
credential schemas.
"""
from alembic import op

revision = "029_sms_catalog_supports_test"
down_revision = "028_operator_sms_credentials"
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
        WHERE category = 'sms'
          AND credential_schema ? 'supports_test'
        """
    )
