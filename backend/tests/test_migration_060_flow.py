"""Migration 060 over wallet-era data, against a REAL database: downgrade to
059, plant what production has (resellers with wallets, a commission rule,
allocations whose codes are disabled, and one that is still live), upgrade,
and check:

  * an allocation whose code is disabled is marked reclaimed, every row gets
    its operator, and none of them counts or ever earns;
  * resellers keep their rows and get no commission of their own (the
    operator default, 0%, applies); operators default to percent / 0;
  * two emails differing only by case are refused from then on;
  * the downgrade and a second upgrade both run cleanly with the data in place.

Runs alembic in a subprocess against DATABASE_URL, so it needs a database of
its own (tests/throwaway_db.py gives every suite a clone). Works from a fresh
chain (the clone is at head) and from a schema-only copy of production at 059:

    MIGRATION_060_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
    SYNC_DATABASE_URL=<the same database, postgresql://>
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

FLOW_DB = os.getenv("MIGRATION_060_FLOW_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB
        or "throwaway" not in os.getenv("SYNC_DATABASE_URL", "").rsplit("/", 1)[-1],
        reason="needs MIGRATION_060_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL, and SYNC_DATABASE_URL",
    ),
]

BACKEND = Path(__file__).resolve().parents[1]
BEFORE = "059_hotspot_service_cut_off"

if FLOW_DB:
    import psycopg2


def alembic(*args):
    res = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=os.environ.copy(),
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-3000:]


def sql(statement, params=None, fetch=True):
    conn = psycopg2.connect(os.environ["SYNC_DATABASE_URL"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if fetch and cur.description else None
    finally:
        conn.close()


def plant_wallet_era() -> dict:
    """At 059: an operator, a plan, two resellers with wallets, a commission
    rule, and three reseller codes: two disabled (as production's are) and
    one still unused."""
    ids = {k: str(uuid.uuid4()) for k in ("op", "plan", "r1", "r2", "v_off1", "v_off2", "v_live")}
    tag = uuid.uuid4().hex[:8]
    sql("INSERT INTO isp_operators (id, name, slug, contact_email, status, billing_status, monthly_fee_ghs) "
        "VALUES (%s, %s, %s, %s, 'approved', 'active', 50)", (ids["op"], f"Mig {tag}", f"mig-{tag}", f"mig-{tag}@throwaway.test"), fetch=False)
    sql("INSERT INTO plans (id, isp_operator_id, name, type, duration_minutes, download_speed_kbps, upload_speed_kbps, price_ghs) "
        "VALUES (%s, %s, 'Hour', 'time', 60, 2048, 1024, 5)", (ids["plan"], ids["op"]), fetch=False)
    for key, email in (("r1", f"reseller1-{tag}@example.com"), ("r2", f"agent1-{tag}@example.com")):
        sql("INSERT INTO resellers (id, isp_operator_id, name, email, password_hash, role, is_active) "
            "VALUES (%s, %s, 'Seed', %s, 'x', 'reseller', false)", (ids[key], ids["op"], email), fetch=False)
        sql("INSERT INTO reseller_wallets (reseller_id, balance_ghs) VALUES (%s, 490.50)", (ids[key],), fetch=False)
    sql("INSERT INTO commission_rules (isp_operator_id, reseller_id, type, value) VALUES (%s, %s, 'percentage', 10)",
        (ids["op"], ids["r1"]), fetch=False)
    for key, status in (("v_off1", "disabled"), ("v_off2", "disabled"), ("v_live", "unused")):
        sql("INSERT INTO vouchers (id, isp_operator_id, plan_id, code, username, password, status, source) "
            "VALUES (%s, %s, %s, %s, %s, 'pw', %s, 'reseller')",
            (ids[key], ids["op"], ids["plan"], f"C-{uuid.uuid4().hex[:12]}", f"U-{uuid.uuid4().hex[:12]}", status), fetch=False)
        sql("INSERT INTO reseller_voucher_allocations (reseller_id, voucher_id, purchase_price_ghs) VALUES (%s, %s, 4.5)",
            (ids["r1"], ids[key]), fetch=False)
    ids["tag"] = tag
    return ids


def allocation(voucher_id):
    [row] = sql("SELECT status, reclaimed_at IS NOT NULL, isp_operator_id::text, allocation_id FROM reseller_voucher_allocations "
                "WHERE voucher_id = %s", (voucher_id,))
    return row


def test_wallet_era_data_through_060_and_back():
    alembic("downgrade", BEFORE)
    ids = plant_wallet_era()
    alembic("upgrade", "head")

    for key in ("v_off1", "v_off2"):
        assert allocation(ids[key]) == ("reclaimed", True, ids["op"], None)
    assert allocation(ids["v_live"]) == ("allocated", False, ids["op"], None)
    assert sql("SELECT commission_type, commission_value, token_version FROM resellers WHERE id = %s", (ids["r1"],)) == [(None, None, 0)]
    assert sql("SELECT reseller_commission_type, reseller_commission_value, reseller_redeem_days FROM isp_operators "
               "WHERE id = %s", (ids["op"],))[0][:2] == ("percent", 0)
    # Wallets and rules stay, untouched.
    assert sql("SELECT balance_ghs FROM reseller_wallets WHERE reseller_id = %s", (ids["r1"],))[0][0] == 490.5
    assert sql("SELECT count(*) FROM commission_rules WHERE reseller_id = %s", (ids["r1"],))[0][0] == 1

    # The live wallet-era code was paid for up front: redeeming it earns nothing.
    sql("UPDATE vouchers SET status = 'active', activated_at = now() WHERE id = %s", (ids["v_live"],), fetch=False)
    assert sql("SELECT count(*) FROM reseller_redemptions WHERE voucher_id = %s", (ids["v_live"],))[0][0] == 0
    # And nothing of it counts in the new views (they only read allocations made since 060).
    assert sql("SELECT count(*) FROM reseller_voucher_allocations va JOIN reseller_allocations ra ON ra.id = va.allocation_id "
               "WHERE va.reseller_id = %s", (ids["r1"],))[0][0] == 0

    with pytest.raises(psycopg2.errors.UniqueViolation):
        sql("INSERT INTO resellers (isp_operator_id, name, email, password_hash, role) VALUES (%s, 'Dup', %s, 'x', 'reseller')",
            (ids["op"], f"RESELLER1-{ids['tag']}@EXAMPLE.COM"), fetch=False)

    # Down and up again, with all of it in place.
    alembic("downgrade", BEFORE)
    assert sql("SELECT count(*) FROM reseller_voucher_allocations WHERE reseller_id = %s", (ids["r1"],))[0][0] == 3
    alembic("upgrade", "head")
    assert allocation(ids["v_off1"]) == ("reclaimed", True, ids["op"], None)
