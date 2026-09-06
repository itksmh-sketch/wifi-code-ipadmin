import uuid
import datetime
from sqlalchemy import CheckConstraint, Column, String, Text, Integer, Float, Boolean, DateTime, ForeignKey, BigInteger, Numeric, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID, INET, MACADDR, ENUM, JSONB
from sqlalchemy.orm import relationship
from src.db.base import Base


class PlatformOwner(Base):
    __tablename__ = "platform_owners"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    name = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_login_at = Column(DateTime(timezone=True), nullable=True)


class ISPOperator(Base):
    __tablename__ = "isp_operators"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    name = Column(String(255), nullable=False)
    slug = Column(String(255), unique=True, nullable=False)
    contact_email = Column(String(255), nullable=False)
    contact_phone = Column(String(64), nullable=True)
    status = Column(
        ENUM("pending", "approved", "suspended", "cancelled", name="isp_operator_status", create_type=False),
        nullable=False,
        server_default="'pending'",
    )
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by_platform_owner_id = Column(UUID(as_uuid=True), ForeignKey("platform_owners.id"), nullable=True)
    monthly_fee_ghs = Column(Numeric(10, 2), nullable=False, server_default="0.00")
    billing_status = Column(
        ENUM("trial", "active", "past_due", "cancelled", name="operator_billing_status", create_type=False),
        nullable=False,
        server_default="'trial'",
    )
    trial_ends_at = Column(DateTime(timezone=True), nullable=True)
    onboarding_checklist = Column(JSONB, nullable=True, server_default="'{}'")
    # Captive-portal branding (all nullable; defaults applied at read time so an
    # unconfigured operator renders identically to the original hardcoded portal).
    portal_display_name = Column(Text, nullable=True)
    logo_url = Column(Text, nullable=True)
    primary_color = Column(Text, nullable=True)
    accent_color = Column(Text, nullable=True)
    background_gradient_start = Column(Text, nullable=True)
    portal_welcome_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class OperatorPaymentCredential(Base):
    __tablename__ = "operator_payment_credentials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id", ondelete="CASCADE"), unique=True, nullable=False)
    provider = Column(
        ENUM("paystack", "flutterwave", "hubtel", name="operator_payment_provider", create_type=False),
        nullable=False,
        server_default="'paystack'",
    )
    public_key_encrypted = Column(Text, nullable=False)
    secret_key_encrypted = Column(Text, nullable=False)
    webhook_secret_encrypted = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="true")
    last_validated_at = Column(DateTime(timezone=True), nullable=True)
    last_validation_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PlatformPaymentCredential(Base):
    """The platform's own payment keys — how operator subscriptions are collected.

    Not to be confused with [[OperatorPaymentCredential]], which is an operator's
    keys for selling vouchers to their own customers. This one is platform-level.

    One row per provider, at most one active (partial unique index on
    ``is_active``). Secrets are Fernet ciphertext and are never returned in full
    by the API — reads are masked to the last 4 characters, same discipline as
    the operator table. When no active row exists the resolver falls back to the
    PLATFORM_BILLING_PAYSTACK_* env vars.

    See src/modules/platform/payment_credentials_service.py.
    """
    __tablename__ = "platform_payment_credentials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    provider = Column(
        ENUM(
            "paystack", "flutterwave", "mtn_momo", "vodafone_cash", "airteltigo",
            name="platform_payment_provider",
            create_type=False,
        ),
        nullable=False,
        server_default="'paystack'",
    )
    public_key_encrypted = Column(Text, nullable=False)
    secret_key_encrypted = Column(Text, nullable=False)
    webhook_secret_encrypted = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="true")
    last_validated_at = Column(DateTime(timezone=True), nullable=True)
    last_validation_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("provider", name="uq_platform_payment_credentials_provider"),
    )


class Town(Base):
    __tablename__ = "towns"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=False)
    name = Column(String(255), nullable=False)
    region = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    sites = relationship("Site", back_populates="town")


class Site(Base):
    __tablename__ = "sites"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=False)
    town_id = Column(UUID(as_uuid=True), ForeignKey("towns.id"), nullable=False)
    name = Column(String(255), nullable=False)
    address = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    town = relationship("Town", back_populates="sites")
    routers = relationship("Router", back_populates="site")
    plans = relationship("Plan", back_populates="site")
    vouchers = relationship("Voucher", back_populates="site")
    payment_transactions = relationship("PaymentTransaction", back_populates="site")


class Router(Base):
    __tablename__ = "routers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=False)
    site_id = Column(UUID(as_uuid=True), ForeignKey("sites.id"), nullable=False)
    name = Column(String(255), nullable=False)
    ip_address = Column(INET, nullable=True)
    nas_identifier = Column(String(255), unique=True, nullable=False)
    nas_secret = Column(Text, nullable=False)
    nas_secret_plain = Column(String(255), nullable=True)
    is_active = Column(Boolean, server_default="true")
    is_online = Column(Boolean, server_default="false", nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)

    # WireGuard VPN tunnel (optional — used when the router is behind NAT)
    wg_enabled = Column(Boolean, server_default="false", nullable=False)
    wg_peer_public_key = Column(String(64), unique=True, nullable=True)
    wg_peer_private_key_encrypted = Column(Text, nullable=True)
    wg_tunnel_ip = Column(INET, nullable=True)
    wg_last_handshake_at = Column(DateTime(timezone=True), nullable=True)
    wg_is_connected = Column(Boolean, server_default="false", nullable=False)

    site = relationship("Site", back_populates="routers")
    sessions = relationship("Session", back_populates="router")
    credentials = relationship("RouterCredential", back_populates="router", uselist=False)
    provision_logs = relationship("RouterProvisionLog", back_populates="router")
    metrics = relationship("RouterMetric", back_populates="router")
    setup_status = relationship(
        "RouterSetupStatus", back_populates="router", uselist=False, cascade="all, delete-orphan"
    )
    wg_ip_allocation = relationship(
        "WgIpAllocation", back_populates="router", uselist=False, cascade="all, delete-orphan"
    )


class PlatformSetting(Base):
    """Platform-owner-managed key/value settings, seeded from .env on first run.

    Editable at runtime (no restart) — e.g. WG_SERVER_ENDPOINT, PLATFORM_APP_URL,
    WEBHOOK_BASE_URL. See [[platform-settings-requirement]].
    """
    __tablename__ = "platform_settings"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ProviderCatalogEntry(Base):
    """Catalog of the payment and SMS providers the platform knows about.

    Rows come only from migration 021 / the re-runnable seed — there is no
    create or delete API.  The platform admin writes exactly two fields:
    ``is_available`` (offer this provider to operators) and, on platform-provided
    SMS entries, ``platform_rate_per_message``.

    ``is_platform_provided`` distinguishes the two SMS models: True means the
    platform's own gateway credentials are used and operators are billed per
    message on their monthly invoice; False means the operator brings their own
    credentials and is billed by that gateway directly.  ``credential_schema``
    carries ``configured_by`` ("operator" or "platform_admin") plus the field
    descriptors the later operator-config UI renders.

    See [[provider-catalog]] and src/modules/platform/provider_catalog.py.
    """
    __tablename__ = "provider_catalog"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    category = Column(
        ENUM("payment", "sms", name="provider_category", create_type=False),
        nullable=False,
    )
    provider_key = Column(String(64), nullable=False)
    display_name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    credential_schema = Column(JSONB, nullable=False, server_default="'{}'")
    # Is the send/charge path actually built? Gates the availability toggle.
    is_integrated = Column(Boolean, nullable=False, server_default="false")
    is_available = Column(Boolean, nullable=False, server_default="false")
    is_platform_provided = Column(Boolean, nullable=False, server_default="false")
    # Only meaningful when is_platform_provided; unused until SMS billing lands.
    platform_rate_per_message = Column(Numeric(10, 4), nullable=True)
    sort_order = Column(Integer, nullable=False, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("category", "provider_key", name="uq_provider_catalog_category_key"),
        CheckConstraint(
            "NOT (is_available AND NOT is_integrated)",
            name="ck_provider_catalog_available_requires_integrated",
        ),
        CheckConstraint(
            "platform_rate_per_message IS NULL OR is_platform_provided",
            name="ck_provider_catalog_rate_requires_platform_provided",
        ),
    )


class WgIpAllocation(Base):
    __tablename__ = "wg_ip_allocations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    router_id = Column(UUID(as_uuid=True), ForeignKey("routers.id", ondelete="CASCADE"), unique=True, nullable=False)
    tunnel_ip = Column(INET, unique=True, nullable=False)
    allocated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    router = relationship("Router", back_populates="wg_ip_allocation")


class Plan(Base):
    __tablename__ = "plans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=False)
    site_id = Column(UUID(as_uuid=True), ForeignKey("sites.id"), nullable=True)
    name = Column(String(255), nullable=False)
    type = Column(ENUM("time", "data", "hybrid", name="plan_type", create_type=False), nullable=False)
    duration_minutes = Column(Integer, nullable=True)
    data_limit_mb = Column(Integer, nullable=True)
    download_speed_kbps = Column(Integer, nullable=False)
    upload_speed_kbps = Column(Integer, nullable=False)
    price_ghs = Column(Numeric(10, 2), nullable=False)
    is_active = Column(Boolean, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    site = relationship("Site", back_populates="plans")
    vouchers = relationship("Voucher", back_populates="plan")
    payment_transactions = relationship("PaymentTransaction", back_populates="plan")


class Voucher(Base):
    __tablename__ = "vouchers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=False)
    plan_id = Column(UUID(as_uuid=True), ForeignKey("plans.id"), nullable=False)
    site_id = Column(UUID(as_uuid=True), ForeignKey("sites.id"), nullable=True)
    code = Column(String(255), unique=True, nullable=False)
    username = Column(String(255), unique=True, nullable=False)
    password = Column(Text, nullable=False)
    status = Column(ENUM("unused", "active", "exhausted", "expired", "disabled", name="voucher_status", create_type=False), nullable=False, server_default="'unused'")
    device_policy = Column(ENUM("single", "multi", name="device_policy", create_type=False), nullable=False, server_default="'single'")
    max_devices = Column(Integer, server_default="1")
    activated_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    data_used_mb = Column(Integer, server_default="0")
    batch_id = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    plan = relationship("Plan", back_populates="vouchers")
    site = relationship("Site", back_populates="vouchers")
    sessions = relationship("Session", back_populates="voucher")
    payment_transactions = relationship("PaymentTransaction", back_populates="voucher")


class Session(Base):
    __tablename__ = "sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=False)
    voucher_id = Column(UUID(as_uuid=True), ForeignKey("vouchers.id"), nullable=False)
    router_id = Column(UUID(as_uuid=True), ForeignKey("routers.id"), nullable=False)
    username = Column(String(255), nullable=False)
    mac_address = Column(MACADDR, nullable=True)
    ip_address = Column(INET, nullable=True)
    nas_ip = Column(INET, nullable=False)
    session_id = Column(String(255), unique=True, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    stopped_at = Column(DateTime(timezone=True), nullable=True)
    terminate_cause = Column(String(255), nullable=True)
    upload_bytes = Column(BigInteger, server_default="0")
    download_bytes = Column(BigInteger, server_default="0")

    voucher = relationship("Voucher", back_populates="sessions")
    router = relationship("Router", back_populates="sessions")


class CoAEvent(Base):
    __tablename__ = "coa_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=False)
    session_id = Column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True)
    voucher_id = Column(UUID(as_uuid=True), ForeignKey("vouchers.id"), nullable=False)
    router_id = Column(UUID(as_uuid=True), ForeignKey("routers.id"), nullable=True)
    event_type = Column(ENUM("disconnect", "coa_update", name="coa_event_type", create_type=False), nullable=False)
    status = Column(ENUM("pending", "sent", "failed", "confirmed", name="coa_event_status", create_type=False), nullable=False, server_default="'pending'")
    attempt_count = Column(Integer, nullable=False, server_default="0")
    last_attempted_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RouterCredential(Base):
    __tablename__ = "router_credentials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    router_id = Column(UUID(as_uuid=True), ForeignKey("routers.id"), unique=True, nullable=False)
    api_username = Column(String(255), nullable=False)
    api_password_encrypted = Column(Text, nullable=False)
    api_port = Column(Integer, nullable=False, server_default="8728")
    use_ssl = Column(Boolean, nullable=False, server_default="false")
    last_connected_at = Column(DateTime(timezone=True), nullable=True)
    connection_status = Column(
        ENUM("unknown", "online", "offline", "auth_failed", "timeout", name="router_connection_status", create_type=False),
        nullable=False,
        server_default="'unknown'",
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    router = relationship("Router", back_populates="credentials")


class RouterProvisionLog(Base):
    __tablename__ = "router_provision_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    router_id = Column(UUID(as_uuid=True), ForeignKey("routers.id"), nullable=False)
    triggered_by = Column(String(255), nullable=False)
    action = Column(
        ENUM(
            "provision",
            "update_radius",
            "update_hotspot",
            "apply_template",
            "reboot",
            "diagnostics",
            "setup_network",
            "setup_hotspot",
            "setup_radius",
            "setup_nat",
            name="router_provision_action",
            create_type=False,
        ),
        nullable=False,
    )
    status = Column(
        ENUM("pending", "running", "success", "failed", name="router_provision_status", create_type=False),
        nullable=False,
        server_default="'pending'",
    )
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
    commands_executed = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    router = relationship("Router", back_populates="provision_logs")


class RouterSetupStatus(Base):
    """Per-router setup wizard state — persists what each section last applied so
    the UI can show status (configured/partial/etc.) even when the router is offline.
    One row per router; created lazily on first detect/apply."""

    __tablename__ = "router_setup_status"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    router_id = Column(UUID(as_uuid=True), ForeignKey("routers.id", ondelete="CASCADE"), unique=True, nullable=False)
    network_status = Column(String(20), nullable=False, server_default="unconfigured")
    network_applied_at = Column(DateTime(timezone=True), nullable=True)
    network_config = Column(JSONB, nullable=True)
    hotspot_status = Column(String(20), nullable=False, server_default="unconfigured")
    hotspot_applied_at = Column(DateTime(timezone=True), nullable=True)
    hotspot_config = Column(JSONB, nullable=True)
    radius_status = Column(String(20), nullable=False, server_default="unconfigured")
    radius_applied_at = Column(DateTime(timezone=True), nullable=True)
    radius_config = Column(JSONB, nullable=True)
    nat_status = Column(String(20), nullable=False, server_default="unconfigured")
    nat_applied_at = Column(DateTime(timezone=True), nullable=True)
    nat_config = Column(JSONB, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    router = relationship("Router", back_populates="setup_status")


class RouterMetric(Base):
    __tablename__ = "router_metrics"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    router_id = Column(UUID(as_uuid=True), ForeignKey("routers.id"), nullable=False)
    collected_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    cpu_load_percent = Column(Integer, nullable=True)
    memory_used_percent = Column(Integer, nullable=True)
    uptime_seconds = Column(BigInteger, nullable=True)
    active_sessions = Column(Integer, nullable=True)
    total_tx_bytes = Column(BigInteger, nullable=True)
    total_rx_bytes = Column(BigInteger, nullable=True)
    board_name = Column(String(255), nullable=True)
    ros_version = Column(String(255), nullable=True)

    router = relationship("Router", back_populates="metrics")


class ConfigTemplate(Base):
    __tablename__ = "config_templates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    template_data = Column(JSONB, nullable=False)
    is_default = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    role = Column(ENUM("superadmin", "admin", "viewer", name="admin_role", create_type=False), nullable=False)
    is_active = Column(Boolean, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_login_at = Column(DateTime(timezone=True), nullable=True)


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=False)
    voucher_id = Column(UUID(as_uuid=True), ForeignKey("vouchers.id"), nullable=True)
    plan_id = Column(UUID(as_uuid=True), ForeignKey("plans.id"), nullable=False)
    site_id = Column(UUID(as_uuid=True), ForeignKey("sites.id"), nullable=False)
    amount_ghs = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(10), nullable=False, server_default="GHS")
    payment_method = Column(
        ENUM("mtn_momo", "vodafone_cash", "airteltigo", "card", name="payment_method", create_type=False),
        nullable=False,
    )
    provider = Column(
        ENUM("mtn", "vodafone", "airteltigo", "paystack", name="payment_provider", create_type=False),
        nullable=False,
    )
    provider_reference = Column(String(255), nullable=True)
    internal_reference = Column(String(255), unique=True, nullable=False)
    phone_number = Column(String(32), nullable=True)
    status = Column(
        ENUM("pending", "success", "failed", "refunded", "reversed", name="payment_status", create_type=False),
        nullable=False,
        server_default="pending",
    )
    failure_reason = Column(Text, nullable=True)
    next_action = Column(String(64), nullable=False, server_default="wait")
    provider_state = Column(String(64), nullable=True)
    payment_channel = Column(String(64), nullable=True)
    display_message = Column(Text, nullable=True)
    provider_payload = Column(JSONB, nullable=True)
    initiated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    webhook_payload = Column(JSONB, nullable=True)
    last_status_check_at = Column(DateTime(timezone=True), nullable=True)
    ip_address = Column(INET, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    voucher = relationship("Voucher", back_populates="payment_transactions")
    plan = relationship("Plan", back_populates="payment_transactions")
    site = relationship("Site", back_populates="payment_transactions")


class Reseller(Base):
    __tablename__ = "resellers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=False)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    phone = Column(String(64), nullable=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(ENUM("reseller", "town_agent", name="reseller_role", create_type=False), nullable=False)
    town_id = Column(UUID(as_uuid=True), ForeignKey("towns.id"), nullable=True)
    site_id = Column(UUID(as_uuid=True), ForeignKey("sites.id"), nullable=True)
    is_active = Column(Boolean, server_default="true", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_login_at = Column(DateTime(timezone=True), nullable=True)


class ResellerWallet(Base):
    __tablename__ = "reseller_wallets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    reseller_id = Column(UUID(as_uuid=True), ForeignKey("resellers.id", ondelete="CASCADE"), unique=True, nullable=False)
    balance_ghs = Column(Numeric(10, 2), server_default="0.00", nullable=False)
    lifetime_topped_up_ghs = Column(Numeric(10, 2), server_default="0.00", nullable=False)
    lifetime_spent_ghs = Column(Numeric(10, 2), server_default="0.00", nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ResellerWalletTransaction(Base):
    __tablename__ = "reseller_wallet_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    wallet_id = Column(UUID(as_uuid=True), ForeignKey("reseller_wallets.id"), nullable=False)
    type = Column(
        ENUM("topup", "purchase", "commission", "adjustment", "refund", name="reseller_wallet_tx_type", create_type=False),
        nullable=False,
    )
    amount_ghs = Column(Numeric(10, 2), nullable=False)
    balance_after_ghs = Column(Numeric(10, 2), nullable=False)
    description = Column(Text, nullable=True)
    reference = Column(String(255), unique=True, nullable=False)
    voucher_id = Column(UUID(as_uuid=True), ForeignKey("vouchers.id"), nullable=True)
    triggered_by = Column(String(32), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CommissionRule(Base):
    __tablename__ = "commission_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=False)
    reseller_id = Column(UUID(as_uuid=True), ForeignKey("resellers.id"), nullable=True)
    plan_id = Column(UUID(as_uuid=True), ForeignKey("plans.id"), nullable=True)
    type = Column(ENUM("flat", "percentage", name="commission_rule_type", create_type=False), nullable=False)
    value = Column(Numeric(10, 4), nullable=False)
    is_active = Column(Boolean, server_default="true", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ResellerVoucherAllocation(Base):
    __tablename__ = "reseller_voucher_allocations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    reseller_id = Column(UUID(as_uuid=True), ForeignKey("resellers.id"), nullable=False)
    voucher_id = Column(UUID(as_uuid=True), ForeignKey("vouchers.id"), unique=True, nullable=False)
    allocated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    sold_at = Column(DateTime(timezone=True), nullable=True)
    sold_to_phone = Column(String(64), nullable=True)
    purchase_price_ghs = Column(Numeric(10, 2), nullable=False)


# ---------------------------------------------------------------------------
# Phase 2 — Operator onboarding, billing, applications
# ---------------------------------------------------------------------------

class OperatorApplication(Base):
    __tablename__ = "operator_applications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_name = Column(String(255), nullable=False)
    contact_name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False)
    phone = Column(String(64), nullable=False)
    region = Column(String(255), nullable=False)
    expected_sites = Column(Integer, nullable=True)
    message = Column(Text, nullable=True)
    status = Column(
        ENUM("pending", "approved", "rejected", name="operator_application_status", create_type=False),
        nullable=False,
        server_default="'pending'",
    )
    reviewed_by_platform_owner_id = Column(UUID(as_uuid=True), ForeignKey("platform_owners.id"), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    rejection_reason = Column(Text, nullable=True)
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OperatorInvoice(Base):
    __tablename__ = "operator_invoices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=False)
    invoice_number = Column(String(64), unique=True, nullable=False)
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)
    amount_ghs = Column(Numeric(10, 2), nullable=False)
    status = Column(
        ENUM("draft", "issued", "paid", "overdue", "waived", name="operator_invoice_status", create_type=False),
        nullable=False,
        server_default="'draft'",
    )
    issued_at = Column(DateTime(timezone=True), nullable=True)
    due_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    payment_reference = Column(String(255), nullable=True)
    paystack_payment_url = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # amount_ghs above is the sum of these; see OperatorInvoiceLineItem.
    line_items = relationship(
        "OperatorInvoiceLineItem",
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="OperatorInvoiceLineItem.sort_order",
    )


class OperatorInvoiceLineItem(Base):
    """One charge on an invoice. The invoice's amount_ghs is the sum of these.

    Add lines only through `billing.service.add_line_item`, which recomputes the
    parent invoice's total in the same flush — assigning `invoice.amount_ghs`
    directly, or inserting a line by hand, lets the stored total drift away from
    what the lines actually sum to.

    `kind` carries sms_usage and adjustment for the SMS billing feature; nothing
    writes them yet. See [[OperatorInvoice]].
    """
    __tablename__ = "operator_invoice_line_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    invoice_id = Column(
        UUID(as_uuid=True), ForeignKey("operator_invoices.id", ondelete="CASCADE"), nullable=False
    )
    kind = Column(
        ENUM("subscription", "sms_usage", "adjustment", name="invoice_line_item_kind", create_type=False),
        nullable=False,
    )
    description = Column(Text, nullable=False)
    quantity = Column(Numeric(12, 4), nullable=False, server_default="1")
    # 4dp, matching provider_catalog.platform_rate_per_message.
    unit_price_ghs = Column(Numeric(10, 4), nullable=False)
    # 2dp — money, and what the invoice total sums.
    amount_ghs = Column(Numeric(10, 2), nullable=False)
    line_metadata = Column("metadata", JSONB, nullable=True)
    sort_order = Column(Integer, nullable=False, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    invoice = relationship("OperatorInvoice", back_populates="line_items")

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_invoice_line_items_quantity_non_negative"),
    )


class OperatorBillingEvent(Base):
    __tablename__ = "operator_billing_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default="gen_random_uuid()")
    isp_operator_id = Column(UUID(as_uuid=True), ForeignKey("isp_operators.id"), nullable=False)
    event_type = Column(
        ENUM(
            "trial_started", "trial_expiry_warning", "trial_expired",
            "invoice_issued", "invoice_paid", "invoice_overdue",
            "grace_period_started", "suspended", "reactivated", "waived",
            name="operator_billing_event_type",
            create_type=False,
        ),
        nullable=False,
    )
    description = Column(Text, nullable=False)
    event_metadata = Column("metadata", JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
