"""Operator status cleanup: cancellation, paused invoicing, three notices.

isp_operators
-------------
cancelled_at / hotspot_service_ends_at
    Set together when the platform cancels an operator, cleared together on
    restore. hotspot_service_ends_at is the end of the wind-down chosen in the
    Cancel dialog (default 30 days; "end now" makes it the cancellation time):
    vouchers already sold keep working until then. Phase B enforces it in the
    RADIUS authorize query; this migration only records it.
    ck_isp_operators_cancellation: both set exactly while status = 'cancelled'.
    Rows already cancelled (none in production) are backfilled from
    updated_at with a 30-day wind-down so the CHECK can be added.

operator_billing_status += 'paused'
    A manual suspension pauses invoicing: generate_monthly_invoices bills only
    'active', and unsuspending puts the operator back on 'trial' or 'active'.
    ck_isp_operators_paused_while_suspended: 'paused' only while suspended.
    The CHECK compares billing_status::text, because a value added by ALTER
    TYPE ... ADD VALUE cannot be used as an enum literal in the transaction
    that added it (PostgreSQL 12+ allows the ADD VALUE itself in one).

platform_notification_templates
-------------------------------
Seeds account_suspended_manual, account_restored and account_cancelled (email
+ SMS). Literal text, not imported from template_catalog, for the reason 056
gives: a migration runs against whatever application code is on disk.
tests/test_migration_058_templates.py pins these rows to the catalog's
DEFAULTS byte for byte. ON CONFLICT DO NOTHING.

Downgrade: an enum value cannot be dropped, so 'paused' stays in the type;
paused rows go back to 'active' (the old code invoiced a manually suspended
operator). Cancelled operators can sign in again under the old code, so list
them before rolling back.

Revision ID: 058_operator_cancellation
Revises: 057_admin_role_staff
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "058_operator_cancellation"
down_revision = "057_admin_role_staff"
branch_labels = None
depends_on = None

NEW_EVENTS = ("account_suspended_manual", "account_restored", "account_cancelled")

TEMPLATE_ROWS = (
    {
        "event_type": "account_suspended_manual",
        "channel": "email",
        "subject": "Account suspended — {isp_name}",
        "body_text": "Your {platform_name} account for {isp_name} has been suspended by the platform team.\nYou can still sign in. New sales and setup changes are paused; vouchers already sold keep working.\nPaying an invoice does not lift this suspension. Questions? Email {support_email}",
        "body_html": "<h2>Account suspended — {isp_name}</h2><p>Your <strong>{platform_name}</strong> account for {isp_name} has been suspended by the platform team.</p><p><strong>What this means:</strong></p><ul><li>You can still sign in and see your account</li><li>New sales through the captive portal and resellers are paused</li><li>New vouchers, plans, routers and other setup changes are paused</li><li>Vouchers already sold and sessions in progress keep working</li></ul><p>Paying an invoice does not lift this suspension. Questions? Email us at <a href='mailto:{support_email}'>{support_email}</a></p>",
    },
    {
        "event_type": "account_suspended_manual",
        "channel": "sms",
        "subject": None,
        "body_text": "{platform_name}: Your {isp_name} account has been suspended by the platform team. New sales and setup changes are paused; vouchers already sold keep working. Contact {support_email}.",
        "body_html": None,
    },
    {
        "event_type": "account_restored",
        "channel": "email",
        "subject": "Account restored — {isp_name}",
        "body_text": "Your {platform_name} account for {isp_name} is active again. Sales and setup changes are open. Sign in to continue.\nQuestions? Email {support_email}",
        "body_html": "<h2>Account restored — {isp_name}</h2><p>Your <strong>{platform_name}</strong> account for {isp_name} is active again. Sales and setup changes are open. Sign in to continue.</p><p>Questions? Email us at <a href='mailto:{support_email}'>{support_email}</a></p>",
    },
    {
        "event_type": "account_restored",
        "channel": "sms",
        "subject": None,
        "body_text": "{platform_name}: Your {isp_name} account is active again. Sales are open; sign in to continue. Questions: {support_email}",
        "body_html": None,
    },
    {
        "event_type": "account_cancelled",
        "channel": "email",
        "subject": "Account closed — {isp_name}",
        "body_text": "Your {platform_name} account for {isp_name} has been closed by the platform team.\nDashboard and reseller sign-in are off and new sales have stopped. Your account data is kept.\nQuestions? Email {support_email}",
        "body_html": "<h2>Account closed — {isp_name}</h2><p>Your <strong>{platform_name}</strong> account for {isp_name} has been closed by the platform team.</p><ul><li>Dashboard and reseller sign-in are off</li><li>New sales through the captive portal and resellers have stopped</li><li>Your account data is kept</li></ul><p>Questions? Email us at <a href='mailto:{support_email}'>{support_email}</a></p>",
    },
    {
        "event_type": "account_cancelled",
        "channel": "sms",
        "subject": None,
        "body_text": "{platform_name}: Your {isp_name} account has been closed. Dashboard and reseller sign-in are off and new sales have stopped. Contact {support_email}.",
        "body_html": None,
    },
)


def upgrade():
    # Fail fast rather than queue behind a long-held lock on isp_operators
    # (and block every sign-in queued behind us). LOCAL: this transaction only.
    op.execute("SET LOCAL lock_timeout = '5s'")

    # ── billing status 'paused' ────────────────────────────────────────────
    op.execute("ALTER TYPE operator_billing_status ADD VALUE IF NOT EXISTS 'paused'")

    # ── cancellation ───────────────────────────────────────────────────────
    op.add_column("isp_operators", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("isp_operators", sa.Column("hotspot_service_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE isp_operators SET cancelled_at = updated_at, "
        "hotspot_service_ends_at = updated_at + interval '30 days' WHERE status = 'cancelled'"
    )
    op.create_check_constraint(
        "ck_isp_operators_cancellation",
        "isp_operators",
        "(status = 'cancelled') = (cancelled_at IS NOT NULL) "
        "AND (cancelled_at IS NULL) = (hotspot_service_ends_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_isp_operators_paused_while_suspended",
        "isp_operators",
        "billing_status::text <> 'paused' OR status = 'suspended'",
    )

    # ── notification templates ─────────────────────────────────────────────
    # An empty list would render as INSERT ... DEFAULT VALUES; refuse anything
    # but the six rows rather than seed a partial set.
    pairs = {(r["event_type"], r["channel"]) for r in TEMPLATE_ROWS}
    if len(TEMPLATE_ROWS) != 6 or pairs != {(e, c) for e in NEW_EVENTS for c in ("email", "sms")}:
        raise RuntimeError(f"058: expected 6 template rows, got {sorted(pairs)}")

    templates = sa.table(
        "platform_notification_templates",
        sa.column("event_type", sa.String),
        sa.column("channel", sa.String),
        sa.column("subject", sa.Text),
        sa.column("body_text", sa.Text),
        sa.column("body_html", sa.Text),
    )
    op.execute(
        postgresql.insert(templates)
        .values(list(TEMPLATE_ROWS))
        .on_conflict_do_nothing(index_elements=["event_type", "channel"])
    )


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "DELETE FROM platform_notification_templates WHERE event_type IN ("
        + ", ".join(f"'{e}'" for e in NEW_EVENTS) + ")"
    )
    op.drop_constraint("ck_isp_operators_paused_while_suspended", "isp_operators", type_="check")
    op.execute("UPDATE isp_operators SET billing_status = 'active' WHERE billing_status::text = 'paused'")
    op.drop_constraint("ck_isp_operators_cancellation", "isp_operators", type_="check")
    op.drop_column("isp_operators", "hotspot_service_ends_at")
    op.drop_column("isp_operators", "cancelled_at")
