"""Resellers on consignment: allocations, commission on redemption, and a
running account per reseller.

The wallet era had resellers pay up front for codes they bought. Now an
operator allocates codes to a reseller, and the reseller owes for a code only
once a customer redeems it, less the commission it earns then.

Commission
----------
isp_operators.reseller_commission_type / _value is the operator default;
resellers.commission_type / _value (both or neither) overrides it. Percent is
0-100 of the price, rounded half-up to the pesewa; fixed is an amount per code,
capped at the price. reseller_commission_ghs() is the one definition.

Allocations
-----------
reseller_allocations: one row per allocation (plan, quantity, the plan price
snapshotted as unit_price_ghs, optional sell-by). reseller_voucher_allocations
gains allocation_id, isp_operator_id and a status (allocated | reclaimed).
Wallet-era rows keep allocation_id NULL: they were paid for, so they never earn
and are not counted. Those whose voucher is already disabled (all of them on
production: the two seed resellers' ten codes) are marked reclaimed.
vouchers.redeem_by is the sell-by; the plan's own clock still starts at first
use.

Redemptions
-----------
A code is redeemed when its voucher goes unused -> active, which happens once:
RADIUS Accounting-Start (freeradius/sql.conf) and the app's transition both
guard on status = 'unused'. Trigger vouchers_reseller_redemption fires on that
transition and calls reseller_redemption_insert(), which records the price,
the commission type, rate and amount, and what is owed. UNIQUE(voucher_id) with
ON CONFLICT DO NOTHING is the backstop. The trigger FAILS OPEN: an error is
raised as a WARNING and the activation goes ahead, because a failure here must
never stop a customer's session being metered. jobs.reseller_redemption_repair
adds any row that is missing, nightly, through the same function (source =
'repair').

Account
-------
reseller_account_entries: payments (cash handed over) and reversals (a
payment's correction). Reversal targets a payment of the same reseller and
operator, for its full amount, at most once (UNIQUE reverses_entry_id); a
reversal can't itself be reversed.

Outstanding = sum(redemptions.owed_ghs) - sum(payments) + sum(reversals).

Append-only
-----------
Redemptions and account entries refuse UPDATE, DELETE and TRUNCATE, as
audit_log does (055) and for the same reason: the app connects as a superuser,
so REVOKE would do nothing, but triggers still fire.

Revision ID: 060_reseller_consignment
Revises: 059_hotspot_service_cut_off
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "060_reseller_consignment"
down_revision = "059_hotspot_service_cut_off"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)
_COMMISSION_TYPES = "('percent', 'fixed')"


def _id():
    return sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _created_at():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


def upgrade():
    # ── commission settings ───────────────────────────────────────────────
    op.add_column("isp_operators", sa.Column("reseller_commission_type", sa.String(10), nullable=False,
                                             server_default="percent"))
    op.add_column("isp_operators", sa.Column("reseller_commission_value", sa.Numeric(10, 2), nullable=False,
                                             server_default="0"))
    op.add_column("isp_operators", sa.Column("reseller_redeem_days", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_isp_operators_reseller_commission", "isp_operators",
        f"reseller_commission_type IN {_COMMISSION_TYPES} AND reseller_commission_value >= 0 "
        "AND (reseller_commission_type <> 'percent' OR reseller_commission_value <= 100)",
    )
    op.create_check_constraint(
        "ck_isp_operators_reseller_redeem_days", "isp_operators",
        "reseller_redeem_days IS NULL OR reseller_redeem_days BETWEEN 1 AND 3650",
    )

    op.add_column("resellers", sa.Column("commission_type", sa.String(10), nullable=True))
    op.add_column("resellers", sa.Column("commission_value", sa.Numeric(10, 2), nullable=True))
    op.add_column("resellers", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint(
        "ck_resellers_commission", "resellers",
        "(commission_type IS NULL) = (commission_value IS NULL) AND (commission_type IS NULL OR ("
        f"commission_type IN {_COMMISSION_TYPES} AND commission_value >= 0 "
        "AND (commission_type <> 'percent' OR commission_value <= 100)))",
    )
    # Backs the case-insensitive sign-in (lower(email) = lower(:typed)).
    op.create_index("uq_resellers_email_lower", "resellers", [sa.text("lower(email)")], unique=True)

    # ── allocations ───────────────────────────────────────────────────────
    op.create_table(
        "reseller_allocations",
        _id(),
        sa.Column("isp_operator_id", _UUID, sa.ForeignKey("isp_operators.id"), nullable=False),
        sa.Column("reseller_id", _UUID, sa.ForeignKey("resellers.id"), nullable=False),
        sa.Column("plan_id", _UUID, sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_ghs", sa.Numeric(10, 2), nullable=False),
        sa.Column("redeem_by", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_admin_id", _UUID, sa.ForeignKey("admin_users.id"), nullable=False),
        _created_at(),
        sa.CheckConstraint("quantity BETWEEN 1 AND 1000", name="ck_reseller_allocations_quantity"),
        sa.CheckConstraint("unit_price_ghs >= 0", name="ck_reseller_allocations_price"),
    )
    op.create_index("ix_reseller_allocations_reseller", "reseller_allocations", ["reseller_id", sa.text("created_at DESC")])

    op.add_column("reseller_voucher_allocations",
                  sa.Column("allocation_id", _UUID, sa.ForeignKey("reseller_allocations.id"), nullable=True))
    op.add_column("reseller_voucher_allocations",
                  sa.Column("isp_operator_id", _UUID, sa.ForeignKey("isp_operators.id"), nullable=True))
    op.add_column("reseller_voucher_allocations",
                  sa.Column("status", sa.String(10), nullable=False, server_default="allocated"))
    op.add_column("reseller_voucher_allocations", sa.Column("reclaimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("reseller_voucher_allocations",
                  sa.Column("reclaimed_by_admin_id", _UUID, sa.ForeignKey("admin_users.id"), nullable=True))
    op.add_column("reseller_voucher_allocations", sa.Column("reclaim_reason", sa.Text(), nullable=True))
    op.execute(
        "UPDATE reseller_voucher_allocations a SET isp_operator_id = r.isp_operator_id "
        "FROM resellers r WHERE r.id = a.reseller_id"
    )
    # Wallet-era codes that are already dead: reclaimed, so nothing counts them
    # as remaining. (Rows with allocation_id NULL never earn either way.)
    op.execute(
        "UPDATE reseller_voucher_allocations a SET status = 'reclaimed', reclaimed_at = now(), "
        "reclaim_reason = 'Wallet-era code, already disabled (migration 060)' "
        "FROM vouchers v WHERE v.id = a.voucher_id AND v.status = 'disabled'"
    )
    op.alter_column("reseller_voucher_allocations", "isp_operator_id", nullable=False)
    op.create_check_constraint(
        "ck_reseller_voucher_allocations_status", "reseller_voucher_allocations",
        "status IN ('allocated', 'reclaimed') AND (status = 'reclaimed') = (reclaimed_at IS NOT NULL)",
    )
    op.create_index("ix_reseller_voucher_allocations_allocation", "reseller_voucher_allocations", ["allocation_id"])
    op.create_index("ix_reseller_voucher_allocations_reseller", "reseller_voucher_allocations", ["reseller_id", "status"])

    op.add_column("vouchers", sa.Column("redeem_by", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_vouchers_unused_redeem_by", "vouchers", ["redeem_by"],
                    postgresql_where=sa.text("status = 'unused' AND redeem_by IS NOT NULL"))

    # ── redemptions ───────────────────────────────────────────────────────
    op.create_table(
        "reseller_redemptions",
        _id(),
        sa.Column("isp_operator_id", _UUID, sa.ForeignKey("isp_operators.id"), nullable=False),
        sa.Column("reseller_id", _UUID, sa.ForeignKey("resellers.id"), nullable=False),
        sa.Column("voucher_id", _UUID, sa.ForeignKey("vouchers.id"), nullable=False, unique=True),
        sa.Column("voucher_allocation_id", _UUID, sa.ForeignKey("reseller_voucher_allocations.id"), nullable=False),
        sa.Column("allocation_id", _UUID, sa.ForeignKey("reseller_allocations.id"), nullable=False),
        sa.Column("plan_id", _UUID, sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("price_ghs", sa.Numeric(10, 2), nullable=False),
        sa.Column("commission_type", sa.String(10), nullable=False),
        sa.Column("commission_value", sa.Numeric(10, 2), nullable=False),
        sa.Column("commission_ghs", sa.Numeric(10, 2), nullable=False),
        sa.Column("owed_ghs", sa.Numeric(10, 2), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        _created_at(),
        sa.CheckConstraint(f"commission_type IN {_COMMISSION_TYPES}", name="ck_reseller_redemptions_type"),
        sa.CheckConstraint("commission_ghs >= 0 AND commission_ghs <= price_ghs AND owed_ghs = price_ghs - commission_ghs",
                           name="ck_reseller_redemptions_amounts"),
        sa.CheckConstraint("source IN ('trigger', 'repair')", name="ck_reseller_redemptions_source"),
    )
    op.create_index("ix_reseller_redemptions_reseller", "reseller_redemptions", ["reseller_id", sa.text("redeemed_at DESC")])

    # ── account ───────────────────────────────────────────────────────────
    op.create_table(
        "reseller_account_entries",
        _id(),
        sa.Column("isp_operator_id", _UUID, sa.ForeignKey("isp_operators.id"), nullable=False),
        sa.Column("reseller_id", _UUID, sa.ForeignKey("resellers.id"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("amount_ghs", sa.Numeric(10, 2), nullable=False),
        sa.Column("reverses_entry_id", _UUID, sa.ForeignKey("reseller_account_entries.id"), nullable=True, unique=True),
        sa.Column("received_on", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("recorded_by_admin_id", _UUID, sa.ForeignKey("admin_users.id"), nullable=False),
        _created_at(),
        sa.CheckConstraint("kind IN ('payment', 'reversal')", name="ck_reseller_account_entries_kind"),
        sa.CheckConstraint("amount_ghs > 0", name="ck_reseller_account_entries_amount"),
        sa.CheckConstraint("(kind = 'reversal') = (reverses_entry_id IS NOT NULL)",
                           name="ck_reseller_account_entries_reversal"),
    )
    op.create_index("ix_reseller_account_entries_reseller", "reseller_account_entries",
                    ["reseller_id", sa.text("created_at DESC")])

    op.execute(
        """
        CREATE FUNCTION reseller_money_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % refused', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$;
        """
    )
    for table in ("reseller_redemptions", "reseller_account_entries"):
        op.execute(f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION reseller_money_append_only();")
        op.execute(f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION reseller_money_append_only();")

    # A reversal names a payment of the same reseller and operator, for its full
    # amount. The target is locked, so two reversals of one payment queue up
    # and the second fails on UNIQUE(reverses_entry_id).
    op.execute(
        """
        CREATE FUNCTION reseller_account_check_reversal() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            target reseller_account_entries%ROWTYPE;
        BEGIN
            IF NEW.kind <> 'reversal' THEN
                RETURN NEW;
            END IF;
            SELECT * INTO target FROM reseller_account_entries WHERE id = NEW.reverses_entry_id FOR UPDATE;
            IF NOT FOUND OR target.kind <> 'payment' OR target.reseller_id <> NEW.reseller_id
               OR target.isp_operator_id <> NEW.isp_operator_id OR target.amount_ghs <> NEW.amount_ghs THEN
                RAISE EXCEPTION 'a reversal must reverse one payment of the same reseller, in full'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$;
        """
    )
    op.execute("CREATE TRIGGER reseller_account_entries_check_reversal BEFORE INSERT ON reseller_account_entries "
               "FOR EACH ROW EXECUTE FUNCTION reseller_account_check_reversal();")

    # ── the redemption hook ───────────────────────────────────────────────
    op.execute(
        """
        CREATE FUNCTION reseller_commission_ghs(ctype text, cvalue numeric, price numeric) RETURNS numeric
        LANGUAGE sql IMMUTABLE AS $$
            SELECT CASE WHEN ctype = 'percent' THEN round(price * cvalue / 100, 2) ELSE LEAST(cvalue, price) END
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION reseller_redemption_insert(p_voucher_id uuid, p_redeemed_at timestamptz, p_source text)
        RETURNS boolean
        LANGUAGE plpgsql AS $$
        DECLARE
            inserted integer;
        BEGIN
            INSERT INTO reseller_redemptions (
                isp_operator_id, reseller_id, voucher_id, voucher_allocation_id, allocation_id, plan_id,
                price_ghs, commission_type, commission_value, commission_ghs, owed_ghs, redeemed_at, source
            )
            SELECT v.isp_operator_id, va.reseller_id, v.id, va.id, ra.id, ra.plan_id,
                   ra.unit_price_ghs, rate.ctype, rate.cvalue, c.amount, ra.unit_price_ghs - c.amount,
                   COALESCE(p_redeemed_at, now()), p_source
              FROM vouchers v
              JOIN reseller_voucher_allocations va
                ON va.voucher_id = v.id AND va.status = 'allocated' AND va.isp_operator_id = v.isp_operator_id
              JOIN reseller_allocations ra
                ON ra.id = va.allocation_id AND ra.isp_operator_id = v.isp_operator_id AND ra.plan_id = v.plan_id
              JOIN resellers r ON r.id = va.reseller_id AND r.isp_operator_id = v.isp_operator_id
              JOIN isp_operators o ON o.id = v.isp_operator_id
             CROSS JOIN LATERAL (
                   SELECT COALESCE(r.commission_type, o.reseller_commission_type) AS ctype,
                          COALESCE(r.commission_value, o.reseller_commission_value) AS cvalue
             ) rate
             CROSS JOIN LATERAL (SELECT reseller_commission_ghs(rate.ctype, rate.cvalue, ra.unit_price_ghs) AS amount) c
             WHERE v.id = p_voucher_id
            ON CONFLICT (voucher_id) DO NOTHING;
            GET DIAGNOSTICS inserted = ROW_COUNT;
            RETURN inserted > 0;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION reseller_record_redemption() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            -- Fail open: the subtransaction rolls back alone and the
            -- activation it rides on commits. The repair job adds the row.
            BEGIN
                PERFORM reseller_redemption_insert(NEW.id, COALESCE(NEW.activated_at, now()), 'trigger');
            EXCEPTION WHEN OTHERS THEN
                RAISE WARNING 'reseller_redemption_failed voucher_id=% sqlstate=% error=%', NEW.id, SQLSTATE, SQLERRM;
            END;
            RETURN NULL;
        END
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER vouchers_reseller_redemption AFTER UPDATE OF status ON vouchers FOR EACH ROW "
        "WHEN (OLD.status = 'unused' AND NEW.status = 'active' AND NEW.source = 'reseller') "
        "EXECUTE FUNCTION reseller_record_redemption();"
    )


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS vouchers_reseller_redemption ON vouchers;")
    op.execute("DROP FUNCTION IF EXISTS reseller_record_redemption();")
    op.execute("DROP FUNCTION IF EXISTS reseller_redemption_insert(uuid, timestamptz, text);")
    op.execute("DROP FUNCTION IF EXISTS reseller_commission_ghs(text, numeric, numeric);")
    # DROP TABLE is not refused by the append-only triggers and takes them along.
    op.drop_table("reseller_account_entries")
    op.drop_table("reseller_redemptions")
    op.execute("DROP FUNCTION IF EXISTS reseller_account_check_reversal();")
    op.execute("DROP FUNCTION IF EXISTS reseller_money_append_only();")
    op.drop_index("ix_vouchers_unused_redeem_by", table_name="vouchers")
    op.drop_column("vouchers", "redeem_by")
    op.drop_index("ix_reseller_voucher_allocations_reseller", table_name="reseller_voucher_allocations")
    op.drop_index("ix_reseller_voucher_allocations_allocation", table_name="reseller_voucher_allocations")
    op.drop_constraint("ck_reseller_voucher_allocations_status", "reseller_voucher_allocations")
    for column in ("reclaim_reason", "reclaimed_by_admin_id", "reclaimed_at", "status", "isp_operator_id", "allocation_id"):
        op.drop_column("reseller_voucher_allocations", column)
    op.drop_index("ix_reseller_allocations_reseller", table_name="reseller_allocations")
    op.drop_table("reseller_allocations")
    op.drop_index("uq_resellers_email_lower", table_name="resellers")
    op.drop_constraint("ck_resellers_commission", "resellers")
    for column in ("token_version", "commission_value", "commission_type"):
        op.drop_column("resellers", column)
    op.drop_constraint("ck_isp_operators_reseller_redeem_days", "isp_operators")
    op.drop_constraint("ck_isp_operators_reseller_commission", "isp_operators")
    for column in ("reseller_redeem_days", "reseller_commission_value", "reseller_commission_type"):
        op.drop_column("isp_operators", column)
