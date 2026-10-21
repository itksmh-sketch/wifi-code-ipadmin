import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

import pytest


BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8000")

# Seeded accounts. The seed gives them random passwords and prints each once,
# so a run passes them in; an unset one skips the test rather than guessing.
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@isp.com")
SEED_RESELLER1_EMAIL = "reseller1@example.com"
SEED_AGENT1_EMAIL = "agent1@example.com"
# Resellers a test creates itself (test_multi_tenancy imports it): a fresh
# password per run.
CREATED_RESELLER_PASSWORD = secrets.token_urlsafe(12)


def env_password(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"{name} is not set (the seed prints it once)")
    return value


def _request(method: str, path: str, *, token: str | None = None, body: dict | None = None, params: dict | None = None):
    url = f"{BASE_URL}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read().decode("utf-8")
            return resp.status, json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8")
        parsed = json.loads(payload) if payload else {}
        return e.code, parsed


def _login_admin() -> str:
    status, body = _request(
        "POST",
        "/api/v1/auth/login",
        body={"email": ADMIN_EMAIL, "password": env_password("ADMIN_PASSWORD")},
    )
    assert status == 200, body
    return body["access_token"]


def _login_reseller(email: str, password: str) -> str:
    status, body = _request(
        "POST",
        "/api/v1/reseller/auth/login",
        body={"email": email, "password": password},
    )
    assert status == 200, body
    return body["access_token"]


def _reseller_id_by_email(admin_token: str, email: str) -> str:
    status, rows = _request("GET", "/api/v1/admin/resellers", token=admin_token)
    assert status == 200, rows
    for row in rows:
        if row["email"].lower() == email.lower():
            return row["id"]
    raise AssertionError(f"Reseller {email} not found")


def test_reseller2_never_sees_reseller1_codes():
    admin = _login_admin()
    reseller2 = _login_reseller(SEED_AGENT1_EMAIL, env_password("SEED_AGENT1_PASSWORD"))
    reseller1_id = _reseller_id_by_email(admin, SEED_RESELLER1_EMAIL)
    status, plans = _request("GET", "/api/v1/plans", token=admin)
    assert status == 200 and plans, plans
    status, allocated = _request("POST", f"/api/v1/admin/resellers/{reseller1_id}/allocations", token=admin,
                                 body={"plan_id": plans[0]["id"], "quantity": 1})
    assert status == 200, allocated
    [code] = allocated["codes"]

    # No reseller id in any reseller path; one passed as a parameter is ignored.
    for params in (None, {"reseller_id": reseller1_id}):
        status, rows = _request("GET", "/api/v1/reseller/codes", token=reseller2, params=params)
        assert status == 200, rows
        assert code not in {row["code"] for row in rows}


def test_admin_token_rejected_on_reseller_endpoint():
    admin = _login_admin()
    status, _ = _request("GET", "/api/v1/reseller/me", token=admin)
    assert status == 401


def test_reseller_token_rejected_on_admin_endpoint():
    reseller2 = _login_reseller(SEED_AGENT1_EMAIL, env_password("SEED_AGENT1_PASSWORD"))
    status, _ = _request("GET", "/api/v1/admin/resellers", token=reseller2)
    assert status == 401
