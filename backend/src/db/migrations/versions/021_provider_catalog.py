"""Provider catalog: the payment and SMS providers the platform knows about.

One row per provider, created here (and refreshed by the seed script) — there is
no create/delete API.  The platform admin toggles `is_available` and, on
platform-provided SMS entries, sets `platform_rate_per_message`.

`is_platform_provided` splits SMS into two billing models: the platform's own
gateway (platform credentials, operators billed per message on their invoice)
versus bring-your-own (operator credentials, billed by that gateway directly).
`platform_rate_per_message` is only meaningful on the former and sits unused
until the SMS billing feature consumes it.

Seeding is idempotent and re-runnable: it refreshes the structural columns but
never overwrites `is_available` or `platform_rate_per_message`.  See
`src/modules/platform/provider_catalog.py` for the catalog contents.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "021_provider_catalog"
down_revision = "020_backstop_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE provider_category AS ENUM ('payment', 'sms')")

    op.create_table(
        "provider_catalog",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("category", postgresql.ENUM(name="provider_category", create_type=False), nullable=False),
        sa.Column("provider_key", sa.String(64), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # {"configured_by": "operator"|"platform_admin",
        #  "fields": [{name,label,type,required,secret}, ...]}
        sa.Column("credential_schema", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        # Is the send/charge path actually built? Gates the availability toggle.
        sa.Column("is_integrated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # Platform admin's switch: is this offered to operators?
        sa.Column("is_available", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # True = platform's own credentials, operators billed per message.
        sa.Column("is_platform_provided", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # Only meaningful when is_platform_provided; NULL until the admin sets it.
        sa.Column("platform_rate_per_message", sa.Numeric(10, 4), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("category", "provider_key", name="uq_provider_catalog_category_key"),
        # Availability can never be switched on for a provider with no working
        # integration.  The API returns 409 for this, but the DB is the backstop
        # so a stray UPDATE can't expose a dead provider to operators either.
        sa.CheckConstraint(
            "NOT (is_available AND NOT is_integrated)",
            name="ck_provider_catalog_available_requires_integrated",
        ),
        # A per-message rate is meaningless on a bring-your-own provider.
        sa.CheckConstraint(
            "platform_rate_per_message IS NULL OR is_platform_provided",
            name="ck_provider_catalog_rate_requires_platform_provided",
        ),
    )

    op.create_index(
        "ix_provider_catalog_category_sort",
        "provider_catalog",
        ["category", "sort_order"],
    )

    from src.modules.platform.provider_catalog import sync_provider_catalog

    sync_provider_catalog(op.get_bind())


def downgrade() -> None:
    op.drop_index("ix_provider_catalog_category_sort", table_name="provider_catalog")
    op.drop_table("provider_catalog")
    op.execute("DROP TYPE provider_category")
