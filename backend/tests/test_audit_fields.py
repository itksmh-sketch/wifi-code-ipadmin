"""The per-model audit field lists (modules/audit/fields.py).

Pure unit test: no server, no DB.
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect as sa_inspect

from src.db import models
from src.modules.audit import fields
from src.modules.audit.redact import CHANGED, REDACTED, is_secret_name


@pytest.mark.parametrize("entry", fields.ALL, ids=lambda e: e.model)
def test_tracked_fields_are_real_columns(entry):
    columns = {c.key for c in sa_inspect(getattr(models, entry.model)).mapper.column_attrs}
    assert set(entry.tracked) <= columns, sorted(set(entry.tracked) - columns)


@pytest.mark.parametrize("entry", fields.ALL, ids=lambda e: e.model)
def test_nothing_secret_is_tracked_or_shown(entry):
    assert entry.shown <= set(entry.tracked)
    assert not [name for name in entry.tracked if is_secret_name(name)]


def test_voucher_credentials_are_never_tracked():
    assert not {"code", "username", "password"} & set(fields.VOUCHER.tracked)


def test_untracked_columns_never_reach_a_row():
    class Obj:
        name, price_ghs, password_hash = "A", 5, "h1"

    before = fields.PLAN.snapshot(Obj())
    Obj.name, Obj.password_hash = "B", "h2"
    assert fields.PLAN.diff(before, fields.PLAN.snapshot(Obj())) == {"name": {"before": "A", "after": "B"}}


def test_hidden_fields_show_as_changed():
    entry = fields._fields("Plan", ("name", "price_ghs"), hidden=("price_ghs",))
    assert entry.diff({"name": "A", "price_ghs": 1}, {"name": "A", "price_ghs": 2}) == {"price_ghs": CHANGED}


def test_secret_detail_keys_are_redacted():
    from src.modules.audit.service import build

    row = build("x", detail={"code_length": 16, "characters": 16})
    assert row.detail == {"code_length": REDACTED, "characters": 16}


# ── Masked voucher code suffix ──────────────────────────────────────────────

def test_mask_code_keeps_only_the_last_four_characters():
    from src.modules.audit.redact import mask_code

    assert mask_code("ABCD-EFGH-JKLM-NV9W") == "…NV9W"
    assert mask_code("AB12-NV9") == "…2NV9"  # dashes don't count
    assert mask_code(None) == "…"


@pytest.mark.parametrize("value", ["ABCD-EFGH-JKLM-NV9W", "ABCDEFGHNV9W", "…ABCDE", "NV9W", "…NV-9W", 1234, None])
def test_code_suffix_key_only_passes_a_masked_suffix(value):
    from src.modules.audit.service import build

    assert build("x", detail={"code_suffix": value}).detail == {"code_suffix": REDACTED}


def test_code_suffix_passes_when_masked_and_only_under_that_key():
    from src.modules.audit.service import build

    row = build("x", detail={"code_suffix": "…NV9W", "voucher_code": "…NV9W", "nested": {"code_suffix": "…AB12"}})
    assert row.detail == {"code_suffix": "…NV9W", "voucher_code": REDACTED, "nested": {"code_suffix": "…AB12"}}
