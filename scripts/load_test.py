import random

from locust import HttpUser, between, task


def _auth_headers(client) -> dict:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@isp.com", "password": "admin123"},
        name="/api/v1/auth/login",
    )
    if resp.status_code != 200:
        return {}
    token = resp.json().get("access_token")
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


class BaseLoadUser(HttpUser):
    abstract = True
    wait_time = between(0.2, 1.0)
    site_id = None
    plan_id = None
    voucher_code = None
    auth_headers = None

    def on_start(self):
        self.auth_headers = _auth_headers(self.client)
        if not self.auth_headers:
            return
        sites = self.client.get("/api/v1/towns", headers=self.auth_headers, name="/api/v1/towns")
        if sites.status_code == 200 and sites.json():
            town_id = sites.json()[0]["id"]
            sites_in_town = self.client.get(
                f"/api/v1/towns/{town_id}/sites",
                headers=self.auth_headers,
                name="/api/v1/towns/{town_id}/sites",
            )
            if sites_in_town.status_code == 200 and sites_in_town.json():
                self.site_id = sites_in_town.json()[0]["id"]

        plans = self.client.get("/api/v1/plans", headers=self.auth_headers, name="/api/v1/plans")
        if plans.status_code == 200 and plans.json():
            self.plan_id = plans.json()[0]["id"]
            if not self.site_id:
                self.site_id = plans.json()[0].get("site_id")

        vouchers = self.client.get("/api/v1/vouchers", headers=self.auth_headers, name="/api/v1/vouchers")
        if vouchers.status_code == 200 and vouchers.json().get("vouchers"):
            self.voucher_code = vouchers.json()["vouchers"][0]["code"]


class PortalPlansUser(BaseLoadUser):
    weight = 50

    @task
    def portal_plans(self):
        if not self.site_id:
            return
        self.client.get(f"/portal/plans?site_id={self.site_id}", name="/portal/plans")


class PortalAuthUser(BaseLoadUser):
    weight = 20

    @task
    def portal_authenticate(self):
        if not self.voucher_code:
            return
        self.client.post(
            "/portal/authenticate",
            data={"code": self.voucher_code},
            name="/portal/authenticate",
        )


class PortalPaymentInitUser(BaseLoadUser):
    weight = 10

    @task
    def portal_initiate_payment(self):
        if not self.plan_id or not self.site_id:
            return
        method = random.choice(["mtn_momo", "vodafone_cash", "airteltigo", "card"])
        self.client.post(
            "/portal/initiate-payment",
            json={
                "plan_id": self.plan_id,
                "site_id": self.site_id,
                "phone": "233244123456",
                "payment_method": method,
            },
            name="/portal/initiate-payment",
        )


class AdminPaymentsUser(BaseLoadUser):
    weight = 5

    @task
    def admin_payments(self):
        if not self.auth_headers:
            return
        self.client.get("/api/v1/payments", headers=self.auth_headers, name="/api/v1/payments")
