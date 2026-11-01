"""Migration 064 rewrites the four trial templates from literals, not from
template_catalog (a migration runs against whatever application code is on
disk; see 058). These tests pin NEW_ROWS to the catalog's DEFAULTS byte for
byte, check OLD_ROWS is the retired text the rows are matched on, and keep the
migration free of application imports. Pure unit tests: no DB.

That OLD_ROWS is exactly what 044 seeded is checked on a real database by
tests/test_billing_fixes_flow.py (a fresh chain ends with the new text).
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from src.modules.notifications.template_catalog import DEFAULTS

PATH = (Path(__file__).resolve().parents[1] / "src" / "db" / "migrations" / "versions"
        / "064_billing_manual_payments.py")
FIELDS = ("event_type", "channel", "subject", "body_text", "body_html")
PAIRS = {(e, c) for e in ("trial_expiry_warning", "trial_expired") for c in ("email", "sms")}


def _migration():
    spec = importlib.util.spec_from_file_location("migration_064", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_same_four_rows_old_and_new_in_the_same_order():
    m = _migration()
    for rows in (m.OLD_ROWS, m.NEW_ROWS):
        assert [(r["event_type"], r["channel"]) for r in rows] == [(r["event_type"], r["channel"]) for r in m.OLD_ROWS]
        assert {(r["event_type"], r["channel"]) for r in rows} == PAIRS
        assert all(set(r) == set(FIELDS) for r in rows)


def test_new_rows_are_byte_identical_to_the_catalog_defaults():
    m = _migration()
    for row in m.NEW_ROWS:
        d = DEFAULTS[(row["event_type"], row["channel"])]
        catalog = {"event_type": d.event, "channel": d.channel, "subject": d.subject,
                   "body_text": d.body_text, "body_html": d.body_html}
        for field in FIELDS:
            assert row[field] == catalog[field], (row["event_type"], row["channel"], field)


def test_old_rows_are_the_retired_read_only_text():
    m = _migration()
    for row in m.OLD_ROWS:
        assert row != {f: getattr(DEFAULTS[(row["event_type"], row["channel"])], {"event_type": "event"}.get(f, f))
                       for f in FIELDS}
    assert any("read-only" in r["body_text"] for r in m.OLD_ROWS)
    assert not any("read-only" in (r["body_text"] + (r["body_html"] or "")) for r in m.NEW_ROWS)


def test_migration_imports_no_application_code():
    imported = set()
    for node in ast.walk(ast.parse(PATH.read_text())):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= {"alembic", "sqlalchemy", "sqlalchemy.dialects"}, imported
