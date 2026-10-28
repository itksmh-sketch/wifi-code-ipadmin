"""verify_charge: only a live-mode Paystack charge settles a platform invoice.

Pure unit tests — no database, no app.
"""
from __future__ import annotations

import pytest

from src.modules.webhooks.charge_verification import ChargeRejection, verify_charge

EXPECTED = 5000  # GHS 50.00


def _payload(event="charge.success", **data):
    body = {"status": "success", "amount": EXPECTED, "currency": "GHS", "domain": "live"}
    body.update(data)
    return {"event": event, "data": {k: v for k, v in body.items() if v is not None}}


def test_live_charge_is_accepted():
    verdict = verify_charge(_payload(), expected_pesewas=EXPECTED)
    assert verdict.accepted
    assert verdict.domain == "live"


def test_live_is_matched_ignoring_case_and_spaces():
    assert verify_charge(_payload(domain=" LIVE "), expected_pesewas=EXPECTED).accepted


def test_test_mode_charge_is_refused():
    verdict = verify_charge(_payload(domain="test"), expected_pesewas=EXPECTED)
    assert not verdict.accepted
    assert verdict.rejection is ChargeRejection.NOT_LIVE
    assert verdict.domain == "test"
    assert "test mode" in verdict.reason


@pytest.mark.parametrize("domain", [None, "", "staging", "livemode"])
def test_missing_or_unknown_mode_is_refused(domain):
    verdict = verify_charge(_payload(domain=domain), expected_pesewas=EXPECTED)
    assert not verdict.accepted
    assert verdict.rejection is ChargeRejection.NOT_LIVE


def test_test_mode_is_refused_even_when_everything_else_is_right_and_overpaid():
    verdict = verify_charge(_payload(domain="test", amount=EXPECTED * 3), expected_pesewas=EXPECTED)
    assert verdict.rejection is ChargeRejection.NOT_LIVE


def test_test_mode_is_named_before_other_faults():
    # A test-mode charge with a bad currency and no amount is reported as test
    # mode: that is the reason a human needs to see first.
    verdict = verify_charge(_payload(domain="test", currency="USD", amount=None), expected_pesewas=EXPECTED)
    assert verdict.rejection is ChargeRejection.NOT_LIVE


def test_a_non_success_event_is_still_just_ignored_in_test_mode():
    verdict = verify_charge(_payload(event="charge.failed", domain="test"), expected_pesewas=EXPECTED)
    assert verdict.rejection is ChargeRejection.NOT_SUCCESS


@pytest.mark.parametrize("data,rejection", [
    ({"currency": "USD"}, ChargeRejection.WRONG_CURRENCY),
    ({"amount": None}, ChargeRejection.MISSING_AMOUNT),
    ({"amount": "50.5"}, ChargeRejection.MALFORMED_AMOUNT),
    ({"amount": EXPECTED - 1}, ChargeRejection.UNDERPAID),
])
def test_live_charges_keep_the_existing_rejections(data, rejection):
    verdict = verify_charge(_payload(**data), expected_pesewas=EXPECTED)
    assert verdict.rejection is rejection
    assert verdict.domain == "live"
