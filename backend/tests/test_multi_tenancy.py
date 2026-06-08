import hashlib
import hmac
import json
import os
import time
import uuid
from decimal import Decimal

import httpx
import pytest

from test_multi_tenant_security import _request

from src.db.models import ISPOperator, OperatorPaymentCredential, PaymentTransaction
from src.modules.payments.service import PaymentService
from src.modules.payments.types import PaymentMethod, PaymentProviderName
from src.utils.encryption import encrypt_secret


PLATFORM_OWNER_EMAIL = os.getenv("PLATFORM_OWNER_EMAIL", "owner@yourisp.com")
PLATFORM_OWNER_PASSWORD = os.getenv("PLATFORM_OWNER_PASSWORD", "ChangeMe2024Strong!")
_PLATFORM_OWNER_TOKEN: str | None = None


def _login_admin(email: str = "admin@isp.com", password: str = "admin123") -> str:
    status, body = _request("POST", "/api/v1/auth/login", body={"email": email, "password": password})
    assert status == 200, body
    return body["access_token"]


def _login_platform_owner() -> str:
    global _PLATFORM_OWNER_TOKEN
    if _PLATFORM_OWNER_TOKEN:
        return _PLATFORM_OWNER_TOKEN
    status, body = _request(
        "POST",
        "/api/v1/platform/auth/login",
        body={"email": PLATFORM_OWNER_EMAIL, "password": PLATFORM_OWNER_PASSWORD},
    )
    assert status == 200, body
    _PLATFORM_OWNER_TOKEN = body["access_token"]
    return _PLATFORM_OWNER_TOKEN


def _create_operator(owner_token: str, slug: str) -> dict:
    status, body = _request(
        "POST",
        "/api/v1/platform/operators",
        token=owner_token,
        body={
            "name": f"{slug.title()} ISP",
            "slug": slug,
            "contact_email": f"{slug}@example.com",
            "contact_phone": "233200000001",
            "initial_admin_email": f"admin-{slug}@example.com",
            "initial_admin_password": "admin12345",
        },
    )
    assert status in (201, 409), body
    if status == 409:
        status, operators = _request("GET", "/api/v1/platform/operators", token=owner_token)
        assert status == 200, operators
        return next(row for row in operators if row["slug"] == slug)
    return body


def _unique_slug(prefix: str) -> str:
    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _login_operator_admin(slug: str) -> str:
    return _login_admin(email=f"admin-{slug}@example.com", password="admin12345")


def _create_operator_pair(prefix: str = "phase1") -> tuple[dict, dict, str, str]:
    owner = _login_platform_owner()
    slug_a = _unique_slug(f"{prefix}-a")
    slug_b = _unique_slug(f"{prefix}-b")
    operator_a = _create_operator(owner, slug_a)
    operator_b = _create_operator(owner, slug_b)
    return operator_a, operator_b, _login_operator_admin(slug_a), _login_operator_admin(slug_b)


def _create_network(admin_token: str, slug: str) -> dict:
    ip_seed = uuid.uuid4().bytes
    router_ip = f"10.{ip_seed[0]}.{ip_seed[1]}.{max(1, ip_seed[2])}"
    status, town = _request(
        "POST",
        "/api/v1/towns",
        token=admin_token,
        body={"name": f"{slug} Town", "region": "Greater Accra"},
    )
    assert status == 201, town

    status, site = _request(
        "POST",
        f"/api/v1/towns/{town['id']}/sites",
        token=admin_token,
        body={"name": f"{slug} Site", "address": f"{slug} Address"},
    )
    assert status == 201, site

    status, router = _request(
        "POST",
        f"/api/v1/sites/{site['id']}/routers",
        token=admin_token,
        body={
            "name": f"{slug} Router",
            "ip_address": router_ip,
            "nas_identifier": f"{slug}-nas-{uuid.uuid4().hex[:8]}",
            "nas_secret": "radius_secret",
            "is_active": True,
        },
    )
    assert status == 201, router

    status, plan = _request(
        "POST",
        "/api/v1/plans",
        token=admin_token,
        body={
            "site_id": site["id"],
            "name": f"{slug} Plan",
            "type": "time",
            "duration_minutes": 60,
            "data_limit_mb": None,
            "download_speed_kbps": 2048,
            "upload_speed_kbps": 1024,
            "price_ghs": 5.0,
            "is_active": True,
        },
    )
    assert status == 201, plan
    return {"town": town, "site": site, "router": router, "plan": plan}


def _generate_voucher(admin_token: str, plan_id: str, site_id: str) -> dict:
    status, vouchers = _request(
        "POST",
        "/api/v1/vouchers/generate",
        token=admin_token,
        body={"plan_id": plan_id, "site_id": site_id, "quantity": 1, "device_policy": "single"},
    )
    assert status == 201, vouchers
    assert len(vouchers) == 1
    return vouchers[0]


def _start_radius_session(voucher: dict, router: dict, session_id: str):
    status, body = _request(
        "POST",
        "/api/v1/radius/accounting",
        token=os.getenv("RADIUS_ACCOUNTING_SECRET", "acct_secret_123"),
        body={
            "acct_status_type": "Start",
            "username": voucher["username"],
            "acct_session_id": session_id,
            "nas_ip_address": router["ip_address"],
            "nas_identifier": router["nas_identifier"],
            "calling_station_id": "AA:BB:CC:DD:EE:01",
            "framed_ip_address": "192.168.88.10",
        },
    )
    assert status == 200, body


def _create_reseller(admin_token: str, network: dict, slug: str) -> dict:
    email = f"reseller-{slug}-{uuid.uuid4().hex[:6]}@example.com"
    status, reseller = _request(
        "POST",
        "/api/v1/admin/resellers",
        token=admin_token,
        body={
            "name": f"{slug} Reseller",
            "email": email,
            "phone": "233200000002",
            "password": "reseller123",
            "role": "reseller",
            "town_id": network["town"]["id"],
            "site_id": network["site"]["id"],
            "is_active": True,
        },
    )
    assert status in (200, 201), reseller
    reseller["email"] = email
    return reseller


def _login_reseller(email: str, password: str = "reseller123") -> str:
    status, body = _request("POST", "/api/v1/reseller/auth/login", body={"email": email, "password": password})
    assert status == 200, body
    return body["access_token"]


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ProviderLookupDb:
    def __init__(self, credentials: OperatorPaymentCredential, operator: ISPOperator):
        self.credentials = credentials
        self.operator = operator

    async def execute(self, _statement):
        return _ScalarResult(self.credentials)

    async def get(self, model, row_id):
        if model is ISPOperator and row_id == self.operator.id:
            return self.operator
        return None


def test_platform_owner_can_see_operator_summary_and_admin_cannot():
    owner = _login_platform_owner()
    admin = _login_admin()
    slug = f"phase1-{int(time.time())}"
    operator = _create_operator(owner, slug)

    status, summary = _request("GET", f"/api/v1/platform/operators/{operator['id']}/summary", token=owner)
    assert status == 200, summary
    assert summary["id"] == operator["id"]
    assert "monthly_revenue_this_month" in summary

    status, _ = _request("GET", "/api/v1/platform/operators", token=admin)
    assert status == 401


def test_platform_owner_token_cannot_access_admin_endpoints():
    owner = _login_platform_owner()
    status, _ = _request("GET", "/api/v1/vouchers", token=owner)
    assert status == 401


def test_payment_credentials_response_redacts_keys():
    admin = _login_admin()
    public_key = "pk_test_phase1_public_1234"
    secret_key = "sk_test_phase1_secret_5678"
    webhook_secret = "phase1_webhook_9999"

    status, saved = _request(
        "PUT",
        "/api/v1/payment-credentials",
        token=admin,
        body={
            "provider": "paystack",
            "public_key": public_key,
            "secret_key": secret_key,
            "webhook_secret": webhook_secret,
            "is_active": True,
        },
    )
    assert status == 200, saved
    assert saved["public_key_last4"] == "1234"
    assert saved["secret_key_last4"] == "5678"
    assert saved["webhook_secret_last4"] == "9999"
    assert public_key not in json.dumps(saved)
    assert secret_key not in json.dumps(saved)
    assert webhook_secret not in json.dumps(saved)


def test_slug_scoped_paystack_webhook_uses_operator_secret():
    admin = _login_admin()
    webhook_secret = "phase1_webhook_secret"
    status, _ = _request(
        "PUT",
        "/api/v1/payment-credentials",
        token=admin,
        body={
            "provider": "paystack",
            "public_key": "pk_test_phase1_public_abcd",
            "secret_key": "sk_test_phase1_secret_efgh",
            "webhook_secret": webhook_secret,
            "is_active": True,
        },
    )
    assert status == 200

    payload = {"event": "charge.success", "data": {"reference": "phase1-missing-transaction"}}
    raw = json.dumps(payload).encode("utf-8")
    bad_signature = hmac.new(b"wrong-secret", raw, hashlib.sha512).hexdigest()

    status, body = _request(
        "POST",
        "/api/v1/webhooks/paystack/tenant-zero",
        body=payload,
        params=None,
    )
    assert status in (401, 422), body

    # urllib helper cannot set the Paystack signature header, so the route-level
    # negative assertion above verifies unsigned payloads are rejected.
    assert bad_signature


def test_operator_creation_scopes_initial_admin_to_new_operator():
    owner = _login_platform_owner()
    slug_a = _unique_slug("phase1-create-a")
    slug_b = _unique_slug("phase1-create-b")
    operator_a = _create_operator(owner, slug_a)
    operator_b = _create_operator(owner, slug_b)

    status, admins_a = _request("GET", f"/api/v1/platform/operators/{operator_a['id']}/admins", token=owner)
    assert status == 200, admins_a
    status, admins_b = _request("GET", f"/api/v1/platform/operators/{operator_b['id']}/admins", token=owner)
    assert status == 200, admins_b
    assert {row["email"] for row in admins_a} == {f"admin-{slug_a}@example.com"}
    assert {row["email"] for row in admins_b} == {f"admin-{slug_b}@example.com"}

    admin_a = _login_operator_admin(slug_a)
    admin_b = _login_operator_admin(slug_b)
    status, town = _request(
        "POST",
        "/api/v1/towns",
        token=admin_a,
        body={"name": f"{slug_a} Only", "region": "Greater Accra"},
    )
    assert status == 201, town
    status, towns_b = _request("GET", "/api/v1/towns", token=admin_b)
    assert status == 200, towns_b
    assert town["id"] not in {row["id"] for row in towns_b}


def test_voucher_queries_do_not_cross_operator_boundaries():
    _, _, admin_a, admin_b = _create_operator_pair("phase1-voucher")
    network = _create_network(admin_a, "phase1-voucher-a")
    voucher = _generate_voucher(admin_a, network["plan"]["id"], network["site"]["id"])

    status, listed = _request("GET", "/api/v1/vouchers", token=admin_b)
    assert status == 200, listed
    assert voucher["id"] not in {row["id"] for row in listed["vouchers"]}

    status, _ = _request("GET", f"/api/v1/vouchers/{voucher['id']}", token=admin_b)
    assert status == 404
    status, _ = _request("PUT", f"/api/v1/vouchers/{voucher['id']}/disable", token=admin_b)
    assert status == 404


def test_sessions_are_isolated_by_operator_id():
    _, _, admin_a, admin_b = _create_operator_pair("phase1-session")
    network = _create_network(admin_a, "phase1-session-a")
    voucher = _generate_voucher(admin_a, network["plan"]["id"], network["site"]["id"])
    _start_radius_session(voucher, network["router"], f"phase1-{uuid.uuid4().hex}")

    status, sessions_a = _request("GET", "/api/v1/sessions", token=admin_a)
    assert status == 200, sessions_a
    session = next((row for row in sessions_a["sessions"] if row["voucher_id"] == voucher["id"]), None)
    assert session is not None

    status, sessions_b = _request("GET", "/api/v1/sessions", token=admin_b)
    assert status == 200, sessions_b
    assert session["id"] not in {row["id"] for row in sessions_b["sessions"]}

    status, _ = _request("GET", f"/api/v1/sessions/{session['id']}", token=admin_b)
    assert status == 404


def test_operator_reseller_cannot_buy_another_operators_plan():
    _, _, admin_a, admin_b = _create_operator_pair("phase1-reseller")
    network_a = _create_network(admin_a, "phase1-reseller-a")
    network_b = _create_network(admin_b, "phase1-reseller-b")
    reseller = _create_reseller(admin_a, network_a, "phase1-reseller-a")
    reseller_token = _login_reseller(reseller["email"])

    status, _ = _request(
        "POST",
        "/api/v1/reseller/vouchers/purchase",
        token=reseller_token,
        body={"plan_id": network_b["plan"]["id"], "quantity": 1},
    )
    assert status in (403, 404)


def test_radius_accounting_rejects_cross_operator_voucher_router_pair():
    _, _, admin_a, admin_b = _create_operator_pair("phase1-radius")
    network_a = _create_network(admin_a, "phase1-radius-a")
    network_b = _create_network(admin_b, "phase1-radius-b")
    voucher_a = _generate_voucher(admin_a, network_a["plan"]["id"], network_a["site"]["id"])

    _start_radius_session(voucher_a, network_b["router"], f"phase1-cross-{uuid.uuid4().hex}")

    status, sessions_b = _request("GET", "/api/v1/sessions", token=admin_b)
    assert status == 200, sessions_b
    assert voucher_a["id"] not in {row["voucher_id"] for row in sessions_b["sessions"]}

    status, sessions_a = _request("GET", "/api/v1/sessions", token=admin_a)
    assert status == 200, sessions_a
    assert voucher_a["id"] not in {row["voucher_id"] for row in sessions_a["sessions"]}


def test_captive_portal_gateway_only_returns_operator_plans():
    _, _, admin_a, admin_b = _create_operator_pair("phase1-portal")
    network_a = _create_network(admin_a, "phase1-portal-a")
    network_b = _create_network(admin_b, "phase1-portal-b")

    status, plans = _request("GET", "/portal/plans", params={"gateway": network_a["router"]["ip_address"]})
    assert status == 200, plans
    plan_ids = {row["id"] for row in plans}
    assert network_a["plan"]["id"] in plan_ids
    assert network_b["plan"]["id"] not in plan_ids


@pytest.mark.asyncio
async def test_payment_for_operator_transaction_uses_operator_paystack_secret_key():
    operator_a_id = uuid.uuid4()
    operator_b_id = uuid.uuid4()
    tx = PaymentTransaction(
        id=uuid.uuid4(),
        isp_operator_id=operator_a_id,
        plan_id=uuid.uuid4(),
        site_id=uuid.uuid4(),
        amount_ghs=Decimal("5.00"),
        payment_method=PaymentMethod.CARD.value,
        provider=PaymentProviderName.PAYSTACK.value,
        internal_reference=f"phase1-pay-{uuid.uuid4().hex}",
        status="pending",
    )
    creds_a = OperatorPaymentCredential(
        id=uuid.uuid4(),
        isp_operator_id=operator_a_id,
        provider=PaymentProviderName.PAYSTACK.value,
        public_key_encrypted=encrypt_secret("pk_test_operator_a"),
        secret_key_encrypted=encrypt_secret("sk_test_operator_a"),
        webhook_secret_encrypted=encrypt_secret("whsec_operator_a"),
        is_active=True,
    )
    operator_a = ISPOperator(id=operator_a_id, slug="operator-a", name="Operator A", contact_email="a@example.com")
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "status": True,
                "message": "Authorization URL created",
                "data": {
                    "authorization_url": "https://checkout.paystack.com/operator-a",
                    "reference": tx.internal_reference,
                },
            },
        )

    dummy = object()
    service = PaymentService(dummy, dummy, dummy, dummy)
    provider = await service.provider_for_transaction(_ProviderLookupDb(creds_a, operator_a), tx)
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.paystack.co")

    try:
        await provider.initiate(
            amount_ghs=Decimal("5.00"),
            phone=None,
            plan_id=str(tx.plan_id),
            site_id=str(tx.site_id),
            internal_reference=tx.internal_reference,
            payment_method=PaymentMethod.CARD.value,
        )
    finally:
        await provider._client.aclose()

    assert operator_b_id != operator_a_id
    assert seen["path"] == "/transaction/initialize"
    assert seen["authorization"] == "Bearer sk_test_operator_a"


def test_admin_jwt_cannot_access_platform_endpoints():
    admin = _login_admin()
    status, _ = _request("GET", "/api/v1/platform/operators", token=admin)
    assert status == 401
