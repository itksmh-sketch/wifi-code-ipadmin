"""Platform billing: payments recorded by hand, and billing from the 1st after
the trial.

operator_invoice_payments
-------------------------
A payment the platform owner received outside Paystack (mobile money, bank
transfer, cash) and recorded against one invoice, for its full amount; or the
reversal of one such payment. Recording a payment marks the invoice paid; a
reversal puts it back to issued/overdue (billing.manual_payments).

Like reseller_account_entries (060) and audit_log (055) the table is
append-only: UPDATE, DELETE and TRUNCATE are refused by trigger (the app
connects as a superuser, so REVOKE would do nothing). A wrong entry is
corrected by a reversal, never edited.

operator_invoice_payments_check (BEFORE INSERT) refuses:
  * an entry whose isp_operator_id is not the invoice's operator;
  * a reversal that is not of a payment of the same invoice, operator and
    amount. The target is locked, so two reversals of one payment queue up and
    the second fails on UNIQUE(reverses_payment_id); a reversal can't itself
    be reversed (its target must be kind 'payment').

operator_billing_event_type += 'payment_reversed'
    ADD VALUE, not used as a literal in this transaction (see 058).

platform_notification_templates
-------------------------------
trial_expiry_warning and trial_expired (email + SMS) no longer say the account
goes read-only and that an invoice must be paid at trial end: nothing makes an
account read-only, and billing now starts on the 1st of the month after the
trial ({first_invoice_date}). A row is rewritten only while it still holds the
old default word for word; a row the platform owner edited is left alone.
Literal text, not imported from template_catalog (see 058);
tests/test_migration_064_templates.py pins NEW_ROWS to the catalog.

Revision: numbered 064 because two other plans have claimed 063.
down_revision must name the head at apply time (062 while neither 063 has
landed).

Downgrade: the enum value stays (it can't be dropped); payment_reversed events
are deleted first so no row holds it. The table goes with its triggers. The
four templates go back to the old text where they still hold the new default.

Revision ID: 064_billing_manual_payments
Revises: 062_portal_login_failures
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "064_billing_manual_payments"
down_revision = "062_portal_login_failures"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)

OLD_ROWS = (
    {
        'event_type': 'trial_expiry_warning',
        'channel': 'email',
        'subject': 'Your free trial ends in {days_remaining} days — {isp_name}',
        'body_text': 'Your {platform_name} trial for {isp_name} ends on {trial_end_date} ({days_remaining} days remaining).\nMonthly fee: GHS {monthly_fee_ghs}\nPay at: {billing_url}\nAfter the trial, your account enters read-only mode until payment.',
        'body_html': "<h2>Your trial ends soon, {isp_name}</h2><p>Your <strong>{platform_name}</strong> free trial ends on <strong>{trial_end_date}</strong> ({days_remaining} days remaining).</p><p>Monthly fee: <strong>GHS {monthly_fee_ghs}</strong></p><p>After the trial, your account will switch to read-only mode until you pay your first invoice.</p><p><a href='{billing_url}' style='background:#2563eb;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px'>Pay Now</a></p>",
    },
    {
        'event_type': 'trial_expiry_warning',
        'channel': 'sms',
        'subject': None,
        'body_text': 'Your {platform_name} trial ends in {days_remaining} days. Monthly fee: GHS {monthly_fee_ghs}. Pay at: {billing_url} to keep full access.',
        'body_html': None,
    },
    {
        'event_type': 'trial_expired',
        'channel': 'email',
        'subject': 'Your trial has ended — action required',
        'body_text': 'Your {platform_name} trial for {isp_name} has ended.\nYour account is now in read-only mode.\nPay your invoice to restore access: {billing_url}',
        'body_html': "<h2>Your trial has ended, {isp_name}</h2><p>Your <strong>{platform_name}</strong> free trial has ended. Your account is now in <strong>read-only mode</strong>.</p><p>Pay your invoice to restore full access:</p><p><a href='{billing_url}' style='background:#2563eb;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px'>Pay Invoice</a></p>",
    },
    {
        'event_type': 'trial_expired',
        'channel': 'sms',
        'subject': None,
        'body_text': 'Your {platform_name} trial has ended. Account is now read-only. Pay your invoice to restore access: {billing_url}',
        'body_html': None,
    },
)

NEW_ROWS = (
    {
        'event_type': 'trial_expiry_warning',
        'channel': 'email',
        'subject': 'Your free trial ends in {days_remaining} days — {isp_name}',
        'body_text': 'Your {platform_name} trial for {isp_name} ends on {trial_end_date} ({days_remaining} days remaining).\nMonthly fee: GHS {monthly_fee_ghs}\nYour account stays fully active. Monthly billing starts on {first_invoice_date}; nothing is due before then.\nBilling: {billing_url}',
        'body_html': "<h2>Your trial ends soon, {isp_name}</h2><p>Your <strong>{platform_name}</strong> free trial ends on <strong>{trial_end_date}</strong> ({days_remaining} days remaining).</p><p>Monthly fee: <strong>GHS {monthly_fee_ghs}</strong></p><p>Your account stays fully active when the trial ends. Monthly billing starts on <strong>{first_invoice_date}</strong>; nothing is due before then.</p><p><a href='{billing_url}' style='background:#2563eb;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px'>View billing</a></p>",
    },
    {
        'event_type': 'trial_expiry_warning',
        'channel': 'sms',
        'subject': None,
        'body_text': 'Your {platform_name} trial ends in {days_remaining} days; your account stays active. Monthly fee: GHS {monthly_fee_ghs}, billed from {first_invoice_date}. Billing: {billing_url}',
        'body_html': None,
    },
    {
        'event_type': 'trial_expired',
        'channel': 'email',
        'subject': 'Your trial has ended — {isp_name}',
        'body_text': 'Your {platform_name} trial for {isp_name} has ended.\nYour account stays fully active.\nMonthly billing starts on {first_invoice_date}, with your first invoice.\nBilling: {billing_url}',
        'body_html': "<h2>Your trial has ended, {isp_name}</h2><p>Your <strong>{platform_name}</strong> free trial has ended. Your account stays fully active.</p><p>Monthly billing starts on <strong>{first_invoice_date}</strong>, with your first invoice.</p><p><a href='{billing_url}' style='background:#2563eb;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px'>View billing</a></p>",
    },
    {
        'event_type': 'trial_expired',
        'channel': 'sms',
        'subject': None,
        'body_text': 'Your {platform_name} trial has ended; your account stays fully active. Monthly billing starts on {first_invoice_date}. Billing: {billing_url}',
        'body_html': None,
    },
)


def _swap_templates(from_rows, to_rows):
    for old, new in zip(from_rows, to_rows):
        assert (old["event_type"], old["channel"]) == (new["event_type"], new["channel"])
        op.get_bind().execute(
            sa.text(
                "UPDATE platform_notification_templates "
                "SET subject = :new_subject, body_text = :new_text, body_html = :new_html, updated_at = now() "
                "WHERE event_type = :event AND channel = :channel AND body_text = :old_text "
                "AND subject IS NOT DISTINCT FROM :old_subject AND body_html IS NOT DISTINCT FROM :old_html"
            ),
            {"event": old["event_type"], "channel": old["channel"],
             "old_subject": old["subject"], "old_text": old["body_text"], "old_html": old["body_html"],
             "new_subject": new["subject"], "new_text": new["body_text"], "new_html": new["body_html"]},
        )


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.execute("ALTER TYPE operator_billing_event_type ADD VALUE IF NOT EXISTS 'payment_reversed'")

    op.create_table(
        "operator_invoice_payments",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("isp_operator_id", _UUID, sa.ForeignKey("isp_operators.id"), nullable=False),
        sa.Column("invoice_id", _UUID, sa.ForeignKey("operator_invoices.id"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("amount_ghs", sa.Numeric(10, 2), nullable=False),
        sa.Column("method", sa.String(10), nullable=True),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("received_on", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reverses_payment_id", _UUID, sa.ForeignKey("operator_invoice_payments.id"),
                  nullable=True, unique=True),
        sa.Column("recorded_by_platform_owner_id", _UUID, sa.ForeignKey("platform_owners.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("kind IN ('payment', 'reversal')", name="ck_operator_invoice_payments_kind"),
        sa.CheckConstraint("amount_ghs > 0", name="ck_operator_invoice_payments_amount"),
        sa.CheckConstraint("(kind = 'reversal') = (reverses_payment_id IS NOT NULL)",
                           name="ck_operator_invoice_payments_reversal"),
        # A payment says how it came in; a reversal has no method of its own.
        sa.CheckConstraint("(kind = 'payment') = (method IS NOT NULL) "
                           "AND (method IS NULL OR method IN ('momo', 'bank', 'cash'))",
                           name="ck_operator_invoice_payments_method"),
        # Mobile money and bank transfers always carry a transaction reference.
        sa.CheckConstraint("method IS NULL OR method = 'cash' OR btrim(coalesce(reference, '')) <> ''",
                           name="ck_operator_invoice_payments_reference"),
        # A reversal always says why.
        sa.CheckConstraint("kind <> 'reversal' OR btrim(coalesce(note, '')) <> ''",
                           name="ck_operator_invoice_payments_reversal_note"),
    )
    op.create_index("ix_operator_invoice_payments_invoice", "operator_invoice_payments",
                    ["invoice_id", "created_at"])

    op.execute(
        """
        CREATE FUNCTION operator_invoice_payments_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % refused', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$;
        """
    )
    op.execute("CREATE TRIGGER operator_invoice_payments_no_update_delete BEFORE UPDATE OR DELETE "
               "ON operator_invoice_payments FOR EACH ROW EXECUTE FUNCTION operator_invoice_payments_append_only();")
    op.execute("CREATE TRIGGER operator_invoice_payments_no_truncate BEFORE TRUNCATE "
               "ON operator_invoice_payments FOR EACH STATEMENT EXECUTE FUNCTION operator_invoice_payments_append_only();")

    op.execute(
        """
        CREATE FUNCTION operator_invoice_payments_check() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            target operator_invoice_payments%ROWTYPE;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM operator_invoices
                           WHERE id = NEW.invoice_id AND isp_operator_id = NEW.isp_operator_id) THEN
                RAISE EXCEPTION 'a payment must be recorded against an invoice of the same operator'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.kind <> 'reversal' THEN
                RETURN NEW;
            END IF;
            SELECT * INTO target FROM operator_invoice_payments WHERE id = NEW.reverses_payment_id FOR UPDATE;
            IF NOT FOUND OR target.kind <> 'payment' OR target.invoice_id <> NEW.invoice_id
               OR target.isp_operator_id <> NEW.isp_operator_id OR target.amount_ghs <> NEW.amount_ghs THEN
                RAISE EXCEPTION 'a reversal must reverse one payment of the same invoice, in full'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$;
        """
    )
    op.execute("CREATE TRIGGER operator_invoice_payments_check BEFORE INSERT ON operator_invoice_payments "
               "FOR EACH ROW EXECUTE FUNCTION operator_invoice_payments_check();")

    _swap_templates(OLD_ROWS, NEW_ROWS)


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    _swap_templates(NEW_ROWS, OLD_ROWS)
    # DROP TABLE is not refused by the append-only triggers and takes them along.
    op.drop_table("operator_invoice_payments")
    op.execute("DROP FUNCTION IF EXISTS operator_invoice_payments_check();")
    op.execute("DROP FUNCTION IF EXISTS operator_invoice_payments_append_only();")
    op.execute("DELETE FROM operator_billing_events WHERE event_type::text = 'payment_reversed'")
