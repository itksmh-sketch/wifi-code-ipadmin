import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql


revision = "010_multi_tenant_foundation"
down_revision = "009_restore_active_status"
branch_labels = None
depends_on = None


TENANT_TABLES = [
    "admin_users",
    "towns",
    "sites",
    "routers",
    "plans",
    "vouchers",
    "sessions",
    "payment_transactions",
    "resellers",
    "commission_rules",
    "coa_events",
]


def _hash_password(password: str) -> str:
    from passlib.context import CryptContext

    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    return pwd_context.hash(password)


def _encrypt_secret(secret: str) -> str:
    from cryptography.fernet import Fernet

    encryption_key = os.getenv("ENCRYPTION_KEY")
    if not encryption_key:
        raise RuntimeError("ENCRYPTION_KEY is required to seed Tenant Zero Paystack credentials")
    return Fernet(encryption_key.encode()).encrypt(secret.encode()).decode()


def upgrade() -> None:
    op.execute("CREATE TYPE isp_operator_status AS ENUM ('pending', 'approved', 'suspended', 'cancelled')")
    op.execute("CREATE TYPE operator_billing_status AS ENUM ('trial', 'active', 'past_due', 'cancelled')")
    op.execute("CREATE TYPE operator_payment_provider AS ENUM ('paystack', 'flutterwave', 'hubtel')")

    op.create_table(
        "platform_owners",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("email", name="uq_platform_owners_email"),
    )
    op.create_index("ix_platform_owners_email", "platform_owners", ["email"], unique=True)

    op.create_table(
        "isp_operators",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("contact_email", sa.String(255), nullable=False),
        sa.Column("contact_phone", sa.String(64), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="isp_operator_status", create_type=False),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "approved_by_platform_owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_owners.id"),
            nullable=True,
        ),
        sa.Column("monthly_fee_ghs", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")),
        sa.Column(
            "billing_status",
            postgresql.ENUM(name="operator_billing_status", create_type=False),
            nullable=False,
            server_default=sa.text("'trial'"),
        ),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("slug", name="uq_isp_operators_slug"),
    )
    op.create_index("ix_isp_operators_slug", "isp_operators", ["slug"], unique=True)
    op.create_index("ix_isp_operators_status", "isp_operators", ["status"])
    op.create_index("ix_isp_operators_billing_status", "isp_operators", ["billing_status"])

    op.create_table(
        "operator_payment_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "isp_operator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("isp_operators.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider",
            postgresql.ENUM(name="operator_payment_provider", create_type=False),
            nullable=False,
            server_default=sa.text("'paystack'"),
        ),
        sa.Column("public_key_encrypted", sa.Text(), nullable=False),
        sa.Column("secret_key_encrypted", sa.Text(), nullable=False),
        sa.Column("webhook_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validation_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("isp_operator_id", name="uq_operator_payment_credentials_operator"),
    )
    op.create_index(
        "ix_operator_payment_credentials_isp_operator_id",
        "operator_payment_credentials",
        ["isp_operator_id"],
        unique=True,
    )
    op.create_index("ix_operator_payment_credentials_provider", "operator_payment_credentials", ["provider"])
    op.create_index("ix_operator_payment_credentials_is_active", "operator_payment_credentials", ["is_active"])

    for table_name in TENANT_TABLES:
        op.add_column(table_name, sa.Column("isp_operator_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            f"fk_{table_name}_isp_operator_id",
            table_name,
            "isp_operators",
            ["isp_operator_id"],
            ["id"],
        )
        op.create_index(f"ix_{table_name}_isp_operator_id", table_name, ["isp_operator_id"])

    op.add_column("config_templates", sa.Column("isp_operator_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_config_templates_isp_operator_id",
        "config_templates",
        "isp_operators",
        ["isp_operator_id"],
        ["id"],
    )
    op.create_index("ix_config_templates_isp_operator_id", "config_templates", ["isp_operator_id"])

    bind = op.get_bind()

    owner_email = os.getenv("PLATFORM_OWNER_EMAIL")
    owner_password = os.getenv("PLATFORM_OWNER_PASSWORD")
    owner_id = None

    if owner_email and owner_password:
        owner_id = bind.execute(
            text(
                """
                INSERT INTO platform_owners (email, password_hash, name, is_active)
                VALUES (:email, :password_hash, :name, true)
                ON CONFLICT (email) DO UPDATE
                SET password_hash = EXCLUDED.password_hash,
                    name = EXCLUDED.name,
                    is_active = true
                RETURNING id
                """
            ),
            {
                "email": owner_email.strip().lower(),
                "password_hash": _hash_password(owner_password),
                "name": "Platform Owner",
            },
        ).scalar_one()

    tenant_zero_id = bind.execute(
        text(
            """
            INSERT INTO isp_operators (
                name,
                slug,
                contact_email,
                status,
                approved_at,
                approved_by_platform_owner_id,
                billing_status
            )
            VALUES (
                'Tenant Zero',
                'tenant-zero',
                :contact_email,
                'approved',
                NOW(),
                :owner_id,
                'active'
            )
            ON CONFLICT (slug) DO UPDATE
            SET name = EXCLUDED.name,
                contact_email = EXCLUDED.contact_email,
                status = 'approved',
                approved_at = COALESCE(isp_operators.approved_at, NOW()),
                approved_by_platform_owner_id = COALESCE(
                    isp_operators.approved_by_platform_owner_id,
                    EXCLUDED.approved_by_platform_owner_id
                ),
                billing_status = 'active',
                updated_at = NOW()
            RETURNING id
            """
        ),
        {
            "contact_email": (owner_email or "owner@tenant-zero.local").strip().lower(),
            "owner_id": owner_id,
        },
    ).scalar_one()

    for table_name in TENANT_TABLES:
        bind.execute(
            text(f"UPDATE {table_name} SET isp_operator_id = :tenant_zero_id WHERE isp_operator_id IS NULL"),
            {"tenant_zero_id": tenant_zero_id},
        )
        op.alter_column(table_name, "isp_operator_id", nullable=False)

    paystack_public_key = os.getenv("PAYSTACK_PUBLIC_KEY")
    paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY")
    paystack_webhook_secret = os.getenv("PAYSTACK_WEBHOOK_SECRET")

    if paystack_public_key and paystack_secret_key:
        bind.execute(
            text(
                """
                INSERT INTO operator_payment_credentials (
                    isp_operator_id,
                    provider,
                    public_key_encrypted,
                    secret_key_encrypted,
                    webhook_secret_encrypted,
                    is_active
                )
                VALUES (
                    :isp_operator_id,
                    'paystack',
                    :public_key_encrypted,
                    :secret_key_encrypted,
                    :webhook_secret_encrypted,
                    true
                )
                ON CONFLICT (isp_operator_id) DO UPDATE
                SET provider = 'paystack',
                    public_key_encrypted = EXCLUDED.public_key_encrypted,
                    secret_key_encrypted = EXCLUDED.secret_key_encrypted,
                    webhook_secret_encrypted = EXCLUDED.webhook_secret_encrypted,
                    is_active = true,
                    updated_at = NOW()
                """
            ),
            {
                "isp_operator_id": tenant_zero_id,
                "public_key_encrypted": _encrypt_secret(paystack_public_key),
                "secret_key_encrypted": _encrypt_secret(paystack_secret_key),
                "webhook_secret_encrypted": _encrypt_secret(paystack_webhook_secret)
                if paystack_webhook_secret
                else None,
            },
        )


def downgrade() -> None:
    op.drop_index("ix_config_templates_isp_operator_id", table_name="config_templates")
    op.drop_constraint("fk_config_templates_isp_operator_id", "config_templates", type_="foreignkey")
    op.drop_column("config_templates", "isp_operator_id")

    for table_name in reversed(TENANT_TABLES):
        op.drop_index(f"ix_{table_name}_isp_operator_id", table_name=table_name)
        op.drop_constraint(f"fk_{table_name}_isp_operator_id", table_name, type_="foreignkey")
        op.drop_column(table_name, "isp_operator_id")

    op.drop_index("ix_operator_payment_credentials_is_active", table_name="operator_payment_credentials")
    op.drop_index("ix_operator_payment_credentials_provider", table_name="operator_payment_credentials")
    op.drop_index("ix_operator_payment_credentials_isp_operator_id", table_name="operator_payment_credentials")
    op.drop_table("operator_payment_credentials")

    op.drop_index("ix_isp_operators_billing_status", table_name="isp_operators")
    op.drop_index("ix_isp_operators_status", table_name="isp_operators")
    op.drop_index("ix_isp_operators_slug", table_name="isp_operators")
    op.drop_table("isp_operators")

    op.drop_index("ix_platform_owners_email", table_name="platform_owners")
    op.drop_table("platform_owners")

    op.execute("DROP TYPE operator_payment_provider")
    op.execute("DROP TYPE operator_billing_status")
    op.execute("DROP TYPE isp_operator_status")
