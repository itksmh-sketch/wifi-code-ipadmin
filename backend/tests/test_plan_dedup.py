"""Plan dedup: an operator may not hold two plans with identical settings.

Two halves:
  * pure unit tests over the filter builder -- no server, no DB. They pin the
    null-safe comparison, since a plain `=` would silently stop deduping
    unlimited-data plans (data_limit_mb NULL) without failing anything else.
  * integration tests over POST/PUT /api/v1/plans against a live server, the
    house style for this suite. Skipped when no server is reachable.
"""
import os
import urllib.error
import urllib.request
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import and_
from sqlalchemy.dialects import postgresql

from src.modules.plans.dedup import duplicate_plan_filters, normalize_price
from test_billing import _approve_application, _login_admin, _submit_application, _unique
from test_multi_tenant_security import BASE_URL, _request


# ---------------------------------------------------------------------------
# Unit: the comparison itself
# ---------------------------------------------------------------------------

def _sql(**overrides) -> str:
    kwargs = dict(
        site_id=None,
        plan_type="time",
        duration_minutes=60,
        data_limit_mb=None,
        download_speed_kbps=2048,
        upload_speed_kbps=1024,
        price_ghs=2.00,
    )
    kwargs.update(overrides)
    filters = duplicate_plan_filters(uuid.uuid4(), **kwargs)
    return str(and_(*filters).compile(dialect=postgresql.dialect()))


def test_nullable_fields_use_null_safe_comparison():
    sql = _sql(duration_minutes=None, data_limit_mb=None, site_id=None)
    # `= NULL` is never true, so plain equality would treat two unlimited plans
    # as different and let the duplicate through.
    assert "plans.duration_minutes IS NOT DISTINCT FROM NULL" in sql
    assert "plans.data_limit_mb IS NOT DISTINCT FROM NULL" in sql
    assert "plans.site_id IS NOT DISTINCT FROM NULL" in sql


def test_nullable_fields_stay_null_safe_when_populated():
    sql = _sql(duration_minutes=60, data_limit_mb=500)
    assert "plans.duration_minutes IS NOT DISTINCT FROM" in sql
    assert "plans.data_limit_mb IS NOT DISTINCT FROM" in sql


def test_comparison_is_tenant_scoped_and_covers_all_six_settings():
    sql = _sql()
    assert "plans.isp_operator_id =" in sql
    for column in ("type", "duration_minutes", "data_limit_mb", "download_speed_kbps",
                   "upload_speed_kbps", "price_ghs"):
        assert f"plans.{column}" in sql


def test_name_is_not_part_of_the_comparison():
    # Same settings under a different name is still a duplicate.
    assert "plans.name" not in _sql()


def test_price_is_normalized_to_the_stored_scale():
    # price_ghs is Numeric(10, 2); an unrounded float would miss its own row.
    assert normalize_price(2) == Decimal("2.00")
    assert normalize_price(2.0) == Decimal("2.00")
    assert normalize_price("2.00") == Decimal("2.00")
    assert normalize_price(2.005) == Decimal("2.01")  # Postgres rounds half up


# ---------------------------------------------------------------------------
# Integration: creation and edit through the API
# ---------------------------------------------------------------------------

def _server_up() -> bool:
    try:
        urllib.request.urlopen(f"{BASE_URL}/health", timeout=3).read()
        return True
    except urllib.error.HTTPError:
        return True  # reachable, just no /health route
    except Exception:
        return False


requires_server = pytest.mark.skipif(
    not _server_up(), reason=f"No server at {BASE_URL} (TEST_BASE_URL)"
)


def _new_operator_token() -> str:
    """Provision a fresh operator via apply -> approve and return its admin token.
    Fresh operators start with no plans, so the tests never collide with seed data."""
    email = f"{_unique('plandedup')}@test.com"
    sub = _submit_application(_unique("PlanDedupISP"), email)
    result = _approve_application(sub["id"])
    return _login_admin(email, result["temp_password"])


@pytest.fixture(scope="module")
def operator_a() -> str:
    return _new_operator_token()


@pytest.fixture(scope="module")
def operator_b() -> str:
    return _new_operator_token()


def _plan_body(name: str, **overrides) -> dict:
    body = {
        "name": name,
        "type": "time",
        "duration_minutes": 60,
        "data_limit_mb": None,
        "download_speed_kbps": 2048,
        "upload_speed_kbps": 1024,
        "price_ghs": 2.00,
        "is_active": True,
    }
    body.update(overrides)
    return body


def _create(token: str, name: str, **overrides):
    return _request("POST", "/api/v1/plans", token=token, body=_plan_body(name, **overrides))


@requires_server
def test_same_settings_same_operator_is_blocked(operator_a):
    status, body = _create(operator_a, _unique("Hourly"), price_ghs=3.00)
    assert status == 201, body

    status, body = _create(operator_a, _unique("Hourly Renamed"), price_ghs=3.00)
    assert status == 409, f"Expected 409, got {status}: {body}"
    assert "already exists" in body.get("detail", "").lower()


@requires_server
def test_same_settings_different_operator_is_allowed(operator_a, operator_b):
    settings = {"price_ghs": 4.00, "duration_minutes": 120}
    status, body = _create(operator_a, _unique("Shared"), **settings)
    assert status == 201, body

    # Dedup is per tenant: operator B's catalogue is untouched by operator A's.
    status, body = _create(operator_b, _unique("Shared"), **settings)
    assert status == 201, f"Expected 201, got {status}: {body}"


@requires_server
def test_different_settings_same_operator_is_allowed(operator_a):
    status, body = _create(operator_a, _unique("Cheap"), price_ghs=5.00)
    assert status == 201, body

    status, body = _create(operator_a, _unique("Pricier"), price_ghs=5.50)
    assert status == 201, f"Expected 201, got {status}: {body}"


@requires_server
def test_null_data_cap_duplicates_are_blocked(operator_a):
    # Both plans leave data_limit_mb NULL (unlimited data); everything else matches.
    settings = {"price_ghs": 6.00, "duration_minutes": 1440, "data_limit_mb": None}
    status, body = _create(operator_a, _unique("Unlimited Day"), **settings)
    assert status == 201, body
    assert body["data_limit_mb"] is None

    status, body = _create(operator_a, _unique("All Day Unlimited"), **settings)
    assert status == 409, f"Expected 409, got {status}: {body}"
    assert "already exists" in body.get("detail", "").lower()


@requires_server
def test_edit_into_an_existing_plans_settings_is_blocked(operator_a):
    status, existing = _create(operator_a, _unique("Target"), price_ghs=7.00)
    assert status == 201, existing
    status, other = _create(operator_a, _unique("Mover"), price_ghs=7.50)
    assert status == 201, other

    status, body = _request(
        "PUT", f"/api/v1/plans/{other['id']}", token=operator_a, body={"price_ghs": 7.00}
    )
    assert status == 409, f"Expected 409, got {status}: {body}"
    assert "already exists" in body.get("detail", "").lower()


@requires_server
def test_edits_that_keep_settings_unique_still_work(operator_a):
    """A plan must not be flagged as its own duplicate: renaming and toggling
    is_active leave the settings untouched and have to keep working."""
    status, plan = _create(operator_a, _unique("Editable"), price_ghs=8.00)
    assert status == 201, plan

    status, body = _request(
        "PUT", f"/api/v1/plans/{plan['id']}", token=operator_a, body={"name": _unique("Renamed")}
    )
    assert status == 200, f"Expected 200, got {status}: {body}"

    status, body = _request(
        "PUT", f"/api/v1/plans/{plan['id']}", token=operator_a, body={"is_active": False}
    )
    assert status == 200, f"Expected 200, got {status}: {body}"

    status, body = _request(
        "PUT", f"/api/v1/plans/{plan['id']}", token=operator_a, body={"price_ghs": 8.25}
    )
    assert status == 200, f"Expected 200, got {status}: {body}"
