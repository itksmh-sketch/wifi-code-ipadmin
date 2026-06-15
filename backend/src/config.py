from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://hotspot_user:hotspot_pass@postgres:5432/hotspot_db"
    sync_database_url: str = "postgresql://hotspot_user:hotspot_pass@postgres:5432/hotspot_db"
    jwt_secret: str = "change_me_in_production"
    jwt_expiration_minutes: int = 60
    platform_owner_jwt_secret: str = "change_me_platform_owner_in_production"
    platform_owner_jwt_expiration_minutes: int = 60
    reseller_jwt_secret: str = "change_me_reseller_in_production"
    reseller_jwt_expiration_minutes: int = 60
    reseller_jwt_issuer: str = "reseller"
    radius_coa_secret: str = "radius_secret_123"
    radius_accounting_secret: str = "acct_secret_123"
    encryption_key: str = "change_me_encryption_key_32chars!!"
    # Signs router-bound captive-portal redirect tokens (see utils/portal_token.py).
    # Kept distinct from the auth secrets so no session JWT is ever valid here.
    portal_token_secret: str = "change_me_portal_token_in_production"
    admin_email: str = "admin@isp.com"
    admin_password: str = "admin123"
    platform_owner_email: str = "owner@yourdomain.com"
    platform_owner_password: str = "change-me-strong-password"
    mtn_momo_base_url: str = ""
    mtn_momo_collection_subscription_key: str = ""
    mtn_momo_api_user: str = ""
    mtn_momo_api_key: str = ""
    mtn_momo_environment: str = "sandbox"
    vodafone_cash_base_url: str = ""
    vodafone_cash_merchant_id: str = ""
    vodafone_cash_api_key: str = ""
    vodafone_cash_callback_url: str = ""
    airteltigo_base_url: str = ""
    airteltigo_client_id: str = ""
    airteltigo_client_secret: str = ""
    airteltigo_callback_url: str = ""
    paystack_public_key: str = ""
    paystack_secret_key: str = ""
    paystack_callback_url: str = ""
    # If unset/unknown, SMS is disabled (payments must still succeed).
    sms_provider: str = ""
    hubtel_client_id: str = ""
    hubtel_client_secret: str = ""
    hubtel_from: str = ""
    africastalking_api_key: str = ""
    africastalking_username: str = ""
    africastalking_from: str = ""
    redis_url: str = "redis://redis:6379"
    webhook_base_url: str = ""
    portal_public_base_url: str = ""
    mikrotik_gateway_login_url: str = "http://10.5.5.1/login"
    log_format: str = "console"
    log_level: str = "INFO"
    radius_public_host: str = ""
    mikrotik_api_timeout: int = 10
    mikrotik_cmd_timeout: int = 30

    # Email
    email_provider: str = ""
    sendgrid_api_key: str = ""
    sendgrid_from_email: str = "noreply@yourplatform.com"
    sendgrid_from_name: str = "YourISP Platform"
    mailgun_api_key: str = ""
    mailgun_domain: str = ""
    mailgun_from_email: str = ""
    platform_support_email: str = "support@yourplatform.com"

    # Platform billing (separate Paystack account)
    platform_billing_paystack_public_key: str = ""
    platform_billing_paystack_secret_key: str = ""
    platform_billing_paystack_webhook_secret: str = ""
    platform_app_url: str = "http://localhost:8000"

    # Billing config
    default_monthly_fee_ghs: str = "200.00"
    trial_days: int = 14
    grace_period_days: int = 14

    # WireGuard VPN tunnel (remote router management)
    wg_server_public_key: str = ""
    wg_server_private_key: str = ""
    wg_server_endpoint: str = "34.122.11.114:51820"  # public IP:port routers dial
    wg_tunnel_network: str = "10.100.0.0/24"          # private network for tunnel IPs
    wg_server_tunnel_ip: str = "10.100.0.1"           # server's tunnel IP (.1)
    wg_interface: str = "wg0"
    wg_manager_url: str = "http://127.0.0.1:8999"     # wg-manager sidecar (localhost only)
    wg_handshake_timeout_seconds: int = 180           # peer considered offline after this

    @property
    def effective_portal_public_base_url(self) -> str:
        base = (self.portal_public_base_url or self.webhook_base_url or "").strip().rstrip("/")
        if base:
            return base
        radius_host = (self.radius_public_host or "").strip()
        if radius_host:
            return f"http://{radius_host}:8000"
        return ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
