import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest

from src.modules.resellers.wallet_service import _pick_commission_rule, _RuleView


@dataclass(frozen=True)
class CaseRule:
    reseller_id: uuid.UUID | None
    plan_id: uuid.UUID | None
    type: str = "percentage"
    value: Decimal = Decimal("10.0000")
    is_active: bool = True


def _view(r: CaseRule) -> _RuleView:
    return _RuleView(
        id=uuid.uuid4(),
        reseller_id=r.reseller_id,
        plan_id=r.plan_id,
        type=r.type,
        value=r.value,
        is_active=r.is_active,
    )


def test_commission_priority_level_1_reseller_and_plan_specific():
    reseller_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    other_reseller = uuid.uuid4()
    other_plan = uuid.uuid4()

    rules = [
        _view(CaseRule(reseller_id=None, plan_id=None, value=Decimal("1.0000"))),  # global
        _view(CaseRule(reseller_id=None, plan_id=plan_id, value=Decimal("2.0000"))),  # plan default
        _view(CaseRule(reseller_id=reseller_id, plan_id=None, value=Decimal("3.0000"))),  # reseller default
        _view(CaseRule(reseller_id=reseller_id, plan_id=plan_id, value=Decimal("4.0000"))),  # most specific
        _view(CaseRule(reseller_id=other_reseller, plan_id=other_plan, value=Decimal("9.0000"))),  # irrelevant
    ]

    picked = _pick_commission_rule(rules, reseller_id=reseller_id, plan_id=plan_id)
    assert picked is not None
    assert picked.reseller_id == reseller_id
    assert picked.plan_id == plan_id
    assert picked.value == Decimal("4.0000")


def test_commission_priority_level_2_reseller_specific_plan_null():
    reseller_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    rules = [
        _view(CaseRule(reseller_id=None, plan_id=None, value=Decimal("1.0000"))),
        _view(CaseRule(reseller_id=None, plan_id=plan_id, value=Decimal("2.0000"))),
        _view(CaseRule(reseller_id=reseller_id, plan_id=None, value=Decimal("3.0000"))),
    ]

    picked = _pick_commission_rule(rules, reseller_id=reseller_id, plan_id=plan_id)
    assert picked is not None
    assert picked.reseller_id == reseller_id
    assert picked.plan_id is None
    assert picked.value == Decimal("3.0000")


def test_commission_priority_level_3_plan_specific_reseller_null():
    reseller_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    rules = [
        _view(CaseRule(reseller_id=None, plan_id=None, value=Decimal("1.0000"))),
        _view(CaseRule(reseller_id=None, plan_id=plan_id, value=Decimal("2.0000"))),
    ]

    picked = _pick_commission_rule(rules, reseller_id=reseller_id, plan_id=plan_id)
    assert picked is not None
    assert picked.reseller_id is None
    assert picked.plan_id == plan_id
    assert picked.value == Decimal("2.0000")


def test_commission_priority_level_4_global_default():
    reseller_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    rules = [
        _view(CaseRule(reseller_id=None, plan_id=None, value=Decimal("1.0000"))),
    ]

    picked = _pick_commission_rule(rules, reseller_id=reseller_id, plan_id=plan_id)
    assert picked is not None
    assert picked.reseller_id is None
    assert picked.plan_id is None
    assert picked.value == Decimal("1.0000")


def test_commission_priority_level_5_no_rule_returns_none():
    reseller_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    picked = _pick_commission_rule([], reseller_id=reseller_id, plan_id=plan_id)
    assert picked is None


def test_inactive_rules_are_ignored():
    reseller_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    rules = [
        _view(CaseRule(reseller_id=None, plan_id=None, value=Decimal("1.0000"), is_active=False)),
        _view(CaseRule(reseller_id=None, plan_id=plan_id, value=Decimal("2.0000"), is_active=False)),
        _view(CaseRule(reseller_id=reseller_id, plan_id=None, value=Decimal("3.0000"))),
    ]

    picked = _pick_commission_rule(rules, reseller_id=reseller_id, plan_id=plan_id)
    assert picked is not None
    assert picked.value == Decimal("3.0000")

