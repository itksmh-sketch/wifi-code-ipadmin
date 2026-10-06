"""Migration 056 seeds its four template rows from literals, not from
template_catalog: a migration runs against whatever application code is on
disk, which may be older than it (the first production apply of 056 imported
the catalog before the new catalog was copied, found no rows, and failed).

These tests pin the literals to the catalog's DEFAULTS byte for byte, and keep
the migration free of application imports. Pure unit tests: no DB.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from src.modules.notifications.template_catalog import DEFAULTS

PATH = (Path(__file__).resolve().parents[1] / "src" / "db" / "migrations" / "versions"
        / "056_admin_login_disable.py")
FIELDS = ("event_type", "channel", "subject", "body_text", "body_html")


def _migration():
    spec = importlib.util.spec_from_file_location("migration_056", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _encoded(value):
    return None if value is None else value.encode("utf-8")


def test_exactly_the_four_new_rows():
    m = _migration()
    pairs = [(r["event_type"], r["channel"]) for r in m.TEMPLATE_ROWS]
    assert len(pairs) == 4 and len(set(pairs)) == 4
    assert set(pairs) == {(e, c) for e in m.NEW_EVENTS for c in ("email", "sms")}
    assert all(set(r) == set(FIELDS) for r in m.TEMPLATE_ROWS)


def test_rows_are_byte_identical_to_the_catalog_defaults():
    m = _migration()
    for row in m.TEMPLATE_ROWS:
        d = DEFAULTS[(row["event_type"], row["channel"])]
        catalog = {"event_type": d.event, "channel": d.channel, "subject": d.subject,
                   "body_text": d.body_text, "body_html": d.body_html}
        for field in FIELDS:
            assert _encoded(row[field]) == _encoded(catalog[field]), (row["event_type"], row["channel"], field)


def test_catalog_has_no_other_rows_for_the_new_events():
    m = _migration()
    assert {k for k in DEFAULTS if k[0] in m.NEW_EVENTS} == {(r["event_type"], r["channel"]) for r in m.TEMPLATE_ROWS}


def test_migration_imports_no_application_code():
    imported = set()
    for node in ast.walk(ast.parse(PATH.read_text())):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= {"alembic", "sqlalchemy", "sqlalchemy.dialects"}, imported
