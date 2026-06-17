"""Backfill routers.nas_secret_plain for rows where it is NULL.

FreeRADIUS loads per-router RADIUS clients from the DB via client_query, which
reads the cleartext shared secret from nas_secret_plain. Migration 012 backfilled
existing rows, but rows created afterwards by paths that don't set the column —
notably the seed script (seed.py created MikroTik-Osu / MikroTik-EastLegon with
only nas_secret) — were left NULL and are silently excluded from client_query.

This re-derives nas_secret_plain by *decrypting the existing nas_secret* (mirrors
012). It never invents a value: a row whose nas_secret can't be decrypted falls
back to the raw stored value (legacy plaintext rows). Idempotent — rows that
already have nas_secret_plain are untouched.
"""
from alembic import op
from sqlalchemy import text

revision = "019_backfill_nas_secret_plain"
down_revision = "018_operator_branding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.utils.encryption import decrypt_secret

    bind = op.get_bind()
    rows = bind.execute(
        text("SELECT id, nas_secret FROM routers WHERE nas_secret_plain IS NULL AND nas_secret IS NOT NULL")
    ).fetchall()
    for row in rows:
        try:
            plain = decrypt_secret(row.nas_secret)
        except Exception:
            # Legacy/test rows where nas_secret was stored as plaintext.
            plain = row.nas_secret
        bind.execute(
            text("UPDATE routers SET nas_secret_plain = :plain WHERE id = :id"),
            {"plain": plain, "id": str(row.id)},
        )


def downgrade() -> None:
    # Forward-only: we never clear a populated nas_secret_plain.
    pass
