"""Operator payment credentials: three encrypted columns -> one encrypted JSON
blob, and single-provider -> multi-provider constraints.

WHY
---
Operators can now choose their bring-your-own payment provider (Paystack or
Flutterwave). Two things blocked that:

  1. `UNIQUE(isp_operator_id)` (a named constraint *and* a separate unique index)
     allowed exactly one credential row per operator.
  2. The dedicated `public_key_encrypted` / `secret_key_encrypted` /
     `webhook_secret_encrypted` columns are Paystack-shaped. Different providers
     need different field sets, and the field list already lives in
     `provider_catalog.credential_schema`.

AFTER
-----
  * `credentials_encrypted` — a single Fernet token wrapping
    `json.dumps({field_name: value}, sort_keys=True)`, keyed by the catalog
    schema's field names. Opaque ciphertext, same discipline as
    `routers.nas_secret`; the "JSON shape" is the decrypted dict, not the column.
  * `UNIQUE(isp_operator_id, provider)` — one row per provider per operator.
  * `UNIQUE(isp_operator_id) WHERE is_active` — at most one active provider.
  * `payment_provider` enum gains `flutterwave` (additive; unused here — the
    credential table keys on `operator_payment_provider`, which already has it).

DATA MIGRATION
--------------
Two live rows today (both Paystack test keys, both active, different operators;
one carries a placeholder webhook secret). Backfill decrypts the old columns and
re-encrypts into the blob. A verification gate re-reads every row and asserts the
blob decrypts with `public_key` + `secret_key` present BEFORE the old columns are
dropped — any failure raises, the transaction rolls back, nothing is lost.
Precedent for in-migration Python + encryption utils: 016_reencrypt_nas_secret.

Reversible: downgrade decrypts the blob back into recreated columns. It cannot
un-add the `flutterwave` enum label (PostgreSQL has no DROP VALUE) — harmless.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "026_operator_payment_credentials_jsonb"
down_revision = "025_operator_fee_payable_check"
branch_labels = None
depends_on = None

_MANDATORY_KEYS = ("public_key", "secret_key")


def upgrade() -> None:
    import json
    from src.utils.encryption import decrypt_secret, encrypt_secret

    bind = op.get_bind()

    # --- payment_provider enum: additive, not used in this migration ----------
    # PostgreSQL 12+ allows ADD VALUE inside a transaction as long as the value
    # is not used in the same transaction (it is not). Matches migration 024.
    op.execute("ALTER TYPE payment_provider ADD VALUE IF NOT EXISTS 'flutterwave'")

    # --- 1. new column, nullable for backfill --------------------------------
    op.add_column(
        "operator_payment_credentials",
        sa.Column("credentials_encrypted", sa.Text(), nullable=True),
    )

    # --- 2. backfill: encrypted columns -> encrypted JSON blob ---------------
    rows = bind.execute(
        text(
            "SELECT id, public_key_encrypted, secret_key_encrypted, "
            "webhook_secret_encrypted FROM operator_payment_credentials"
        )
    ).fetchall()
    for row in rows:
        blob = {
            "public_key": decrypt_secret(row.public_key_encrypted),
            "secret_key": decrypt_secret(row.secret_key_encrypted),
        }
        if row.webhook_secret_encrypted:
            blob["webhook_secret"] = decrypt_secret(row.webhook_secret_encrypted)
        bind.execute(
            text(
                "UPDATE operator_payment_credentials "
                "SET credentials_encrypted = :e WHERE id = :id"
            ),
            {
                "e": encrypt_secret(
                    json.dumps(blob, sort_keys=True, separators=(",", ":"))
                ),
                "id": row.id,
            },
        )

    # --- 3. verification gate (runs BEFORE anything destructive) -------------
    for row in bind.execute(
        text("SELECT id, credentials_encrypted FROM operator_payment_credentials")
    ).fetchall():
        if not row.credentials_encrypted:
            raise RuntimeError(
                f"operator_payment_credentials {row.id}: credentials_encrypted "
                "not populated — aborting migration, old columns left intact"
            )
        data = json.loads(decrypt_secret(row.credentials_encrypted))
        missing = [k for k in _MANDATORY_KEYS if not data.get(k)]
        if missing:
            raise RuntimeError(
                f"operator_payment_credentials {row.id}: converted blob missing "
                f"{missing} — aborting migration, old columns left intact"
            )

    # --- 4. lock the column down, drop the Paystack-shaped columns ----------
    op.alter_column(
        "operator_payment_credentials", "credentials_encrypted", nullable=False
    )
    op.drop_column("operator_payment_credentials", "public_key_encrypted")
    op.drop_column("operator_payment_credentials", "secret_key_encrypted")
    op.drop_column("operator_payment_credentials", "webhook_secret_encrypted")

    # --- 5. constraint swap: per-operator -> per-(operator, provider) --------
    op.drop_constraint(
        "uq_operator_payment_credentials_operator",
        "operator_payment_credentials",
        type_="unique",
    )
    # Standalone unique index from migration 010 (not a constraint backer).
    op.drop_index(
        "ix_operator_payment_credentials_isp_operator_id",
        table_name="operator_payment_credentials",
    )
    op.create_unique_constraint(
        "uq_operator_payment_credentials_operator_provider",
        "operator_payment_credentials",
        ["isp_operator_id", "provider"],
    )
    op.create_index(
        "uq_operator_payment_credentials_one_active",
        "operator_payment_credentials",
        ["isp_operator_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    # Re-add isp_operator_id as a plain (non-unique) lookup index.
    op.create_index(
        "ix_operator_payment_credentials_isp_operator_id",
        "operator_payment_credentials",
        ["isp_operator_id"],
    )


def downgrade() -> None:
    import json
    from src.utils.encryption import decrypt_secret, encrypt_secret

    bind = op.get_bind()

    op.drop_index(
        "ix_operator_payment_credentials_isp_operator_id",
        table_name="operator_payment_credentials",
    )
    op.drop_index(
        "uq_operator_payment_credentials_one_active",
        table_name="operator_payment_credentials",
    )
    op.drop_constraint(
        "uq_operator_payment_credentials_operator_provider",
        "operator_payment_credentials",
        type_="unique",
    )

    op.add_column(
        "operator_payment_credentials",
        sa.Column("public_key_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "operator_payment_credentials",
        sa.Column("secret_key_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "operator_payment_credentials",
        sa.Column("webhook_secret_encrypted", sa.Text(), nullable=True),
    )

    for row in bind.execute(
        text("SELECT id, credentials_encrypted FROM operator_payment_credentials")
    ).fetchall():
        data = json.loads(decrypt_secret(row.credentials_encrypted))
        bind.execute(
            text(
                "UPDATE operator_payment_credentials SET "
                "public_key_encrypted = :p, secret_key_encrypted = :s, "
                "webhook_secret_encrypted = :w WHERE id = :id"
            ),
            {
                "p": encrypt_secret(data["public_key"]),
                "s": encrypt_secret(data["secret_key"]),
                "w": encrypt_secret(data["webhook_secret"])
                if data.get("webhook_secret")
                else None,
                "id": row.id,
            },
        )

    op.alter_column(
        "operator_payment_credentials", "public_key_encrypted", nullable=False
    )
    op.alter_column(
        "operator_payment_credentials", "secret_key_encrypted", nullable=False
    )
    op.drop_column("operator_payment_credentials", "credentials_encrypted")

    op.create_unique_constraint(
        "uq_operator_payment_credentials_operator",
        "operator_payment_credentials",
        ["isp_operator_id"],
    )
    op.create_index(
        "ix_operator_payment_credentials_isp_operator_id",
        "operator_payment_credentials",
        ["isp_operator_id"],
        unique=True,
    )
    # 'flutterwave' stays in the payment_provider enum — PostgreSQL cannot remove
    # an enum value. An unused label is harmless.
