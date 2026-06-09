import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8000")


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
        body={"email": "admin@isp.com", "password": "admin123"},
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


def test_reseller2_cannot_get_reseller1_voucher_by_id():
    admin = _login_admin()
    reseller2 = _login_reseller("agent1@example.com", "agent123")
    reseller1_id = _reseller_id_by_email(admin, "reseller1@example.com")

    status, vouchers = _request("GET", f"/api/v1/admin/resellers/{reseller1_id}/vouchers", token=admin)
    assert status == 200, vouchers
    assert len(vouchers) > 0, "Expected seeded reseller1 vouchers"
    voucher_id = vouchers[0]["voucher_id"]

    status, _ = _request("GET", f"/api/v1/reseller/vouchers/{voucher_id}", token=reseller2)
    assert status == 404


def test_wallet_transactions_ignore_foreign_wallet_id():
    reseller1 = _login_reseller("reseller1@example.com", "reseller123")
    reseller2 = _login_reseller("agent1@example.com", "agent123")

    status, wallet1 = _request("GET", "/api/v1/reseller/wallet", token=reseller1)
    assert status == 200, wallet1
    wallet1_id = wallet1["wallet_id"]
    assert wallet1_id is not None

    status, txs_default = _request("GET", "/api/v1/reseller/wallet/transactions", token=reseller2)
    assert status == 200, txs_default
    default_ids = {row["id"] for row in txs_default}

    status, txs_forced = _request(
        "GET",
        "/api/v1/reseller/wallet/transactions",
        token=reseller2,
        params={"wallet_id": wallet1_id},
    )
    assert status == 200, txs_forced
    forced_ids = {row["id"] for row in txs_forced}
    assert forced_ids == default_ids


def test_reseller2_cannot_purchase_plan_outside_scope():
    admin = _login_admin()
    status, resellers = _request("GET", "/api/v1/admin/resellers", token=admin)
    assert status == 200, resellers
    reseller1 = next((r for r in resellers if r["email"].lower() == "reseller1@example.com"), None)
    assert reseller1 is not None
    assert reseller1.get("town_id")
    assert reseller1.get("site_id")

    # Create a separate site and reseller so we can verify cross-site isolation.
    status, new_site = _request(
        "POST",
        f"/api/v1/towns/{reseller1['town_id']}/sites",
        token=admin,
        body={"name": "Security Test Site", "address": "Isolation Check Address"},
    )
    assert status == 201, new_site

    test_email = f"security_reseller2_{int(time.time())}@example.com"
    status, _ = _request(
        "POST",
        "/api/v1/admin/resellers",
        token=admin,
        body={
            "name": "Security Reseller 2",
            "email": test_email,
            "phone": "233200000099",
            "password": "reseller123",
            "role": "reseller",
            "town_id": reseller1["town_id"],
            "site_id": new_site["id"],
            "is_active": True,
        },
    )
    assert status in (200, 201)

    reseller2 = _login_reseller(test_email, "reseller123")

    status, all_plans = _request("GET", "/api/v1/plans", token=admin)
    assert status == 200, all_plans
    forbidden_plan = next((p for p in all_plans if p.get("site_id") == reseller1["site_id"]), None)
    if forbidden_plan is None:
        status, created = _request(
            "POST",
            "/api/v1/plans",
            token=admin,
            body={
                "site_id": reseller1["site_id"],
                "name": "Security forbidden plan",
                "type": "time",
                "duration_minutes": 30,
                "data_limit_mb": None,
                "download_speed_kbps": 2048,
                "upload_speed_kbps": 1024,
                "price_ghs": 1.0,
                "is_active": True,
            },
        )
        assert status == 201, created
        forbidden_plan = created

    status, _ = _request(
        "POST",
        "/api/v1/reseller/vouchers/purchase",
        token=reseller2,
        body={"plan_id": forbidden_plan["id"], "quantity": 1},
    )
    assert status == 403


def test_admin_token_rejected_on_reseller_endpoint():
    admin = _login_admin()
    status, _ = _request("GET", "/api/v1/reseller/me", token=admin)
    assert status == 401


def test_reseller_token_rejected_on_admin_endpoint():
    reseller2 = _login_reseller("agent1@example.com", "agent123")
    status, _ = _request("GET", "/api/v1/admin/resellers", token=reseller2)
    assert status == 401
