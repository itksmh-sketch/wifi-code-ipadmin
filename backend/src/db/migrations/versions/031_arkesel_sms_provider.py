"""Add Arkesel as a bring-your-own SMS provider.

Two changes, both additive:

  * ``operator_sms_provider`` enum gains ``'arkesel'``. PostgreSQL 12+ allows
    ``ADD VALUE`` inside a transaction as long as the new value is not *used* in
    the same transaction — it is not (``provider_catalog.provider_key`` is a
    plain ``varchar``, not this enum). Matches migrations 024 and 026.
  * ``sync_provider_catalog`` upserts the new ``(sms, arkesel)`` catalog row from
    ``src/modules/platform/provider_catalog.py`` — ``is_integrated = true``,
    ``is_available = false`` (the platform admin flips availability after a live
    round-trip, same gate as every other provider). The sync never writes
    ``is_available`` or ``platform_rate_per_message``.

No data migration: zero rows reference the new value.

Reversible-ish: PostgreSQL cannot drop an enum value, so downgrade only removes
the catalog row. The unused ``'arkesel'`` enum label is harmless.
"""
from alembic import op

revision = "031_arkesel_sms_provider"
down_revision = "030_payment_catalog_supports_test"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE operator_sms_provider ADD VALUE IF NOT EXISTS 'arkesel'")

    from src.modules.platform.provider_catalog import sync_provider_catalog

    sync_provider_catalog(op.get_bind())


def downgrade() -> None:
    op.execute(
        "DELETE FROM provider_catalog WHERE category = 'sms' AND provider_key = 'arkesel'"
    )
