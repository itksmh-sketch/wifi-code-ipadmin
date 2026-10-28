"""vouchers.code_key: the code as the portal matches it.

A code is printed in dash-separated groups ("ABCD-EFGH-..."), and the captive
portal matched the typed text exactly after strip + upper, so "abcdefgh..."
or "ABCD EFGH ..." was an unknown code. code_key is the code with everything
but ASCII letters and digits removed, upper-cased. The portal looks codes up
by it, so old codes work with or without dashes, spaces and in any case, and
the new numeric reseller codes ("1234-5678-9012") can be typed as twelve
digits.

It is a STORED generated column, so every path that writes vouchers.code (the
app, the seed, RADIUS-side tooling, a hand-written INSERT) gets it right with
no code of its own. src.modules.vouchers.engine.code_key() is the same
expression in Python; tests keep the two in step.

UNIQUE, platform-wide like vouchers_code_key: two codes that differ only in
dashes or case would make a typed code ambiguous. Production had 0 such
collisions among codes and usernames when this was written (2026-10-25).

No FreeRADIUS change: the router logs in with vouchers.username, which the
portal hands back; RADIUS never sees the typed code.

Revision ID: 061_voucher_code_key
Revises: 060_reseller_consignment
"""
from alembic import op

revision = "061_voucher_code_key"
down_revision = "060_reseller_consignment"
branch_labels = None
depends_on = None

CODE_KEY_SQL = "upper(regexp_replace(code, '[^A-Za-z0-9]', '', 'g'))"


def upgrade() -> None:
    op.execute(f"ALTER TABLE vouchers ADD COLUMN code_key varchar(255) GENERATED ALWAYS AS ({CODE_KEY_SQL}) STORED")
    # A code with no letters or digits at all would key to ''. None exist and
    # the generator can't make one; refuse it rather than let it match ''.
    op.execute("ALTER TABLE vouchers ADD CONSTRAINT ck_vouchers_code_key_not_empty CHECK (code_key <> '')")
    op.execute("CREATE UNIQUE INDEX ux_vouchers_code_key ON vouchers (code_key)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_vouchers_code_key")
    op.execute("ALTER TABLE vouchers DROP CONSTRAINT IF EXISTS ck_vouchers_code_key_not_empty")
    op.execute("ALTER TABLE vouchers DROP COLUMN IF EXISTS code_key")
