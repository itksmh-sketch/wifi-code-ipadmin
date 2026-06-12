"""Re-encrypt routers.nas_secret values that were persisted as plaintext.

A create_router double-commit previously flushed the plaintext secret back over
the encrypted nas_secret column, so existing rows hold plaintext. This migration
re-encrypts any such row (idempotent — already-encrypted rows decrypt cleanly and
are skipped). nas_secret_plain is left untouched; FreeRADIUS reads it directly.
"""
from alembic import op
from sqlalchemy import text

revision = "016_reencrypt_nas_secret"
down_revision = "015_router_setup_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.utils.encryption import decrypt_secret, encrypt_secret

    bind = op.get_bind()
    rows = bind.execute(text("SELECT id, nas_secret, nas_secret_plain FROM routers")).fetchall()
    for row in rows:
        nas_secret = row.nas_secret
        try:
            decrypt_secret(nas_secret)
            continue  # already a valid Fernet token — leave it
        except Exception:
            pass
        # Plaintext source of truth is nas_secret_plain; fall back to the raw value.
        plaintext = row.nas_secret_plain or nas_secret
        bind.execute(
            text("UPDATE routers SET nas_secret = :enc WHERE id = :id"),
            {"enc": encrypt_secret(plaintext), "id": str(row.id)},
        )


def downgrade() -> None:
    # No-op: re-encrypting is forward-only; we never restore plaintext.
    pass
