import uuid
import datetime
from sqlalchemy import Column, String, Text, Integer, Float, Boolean, DateTime, ForeignKey, BigInteger, Numeric, func
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
    ip_address = Column(INET, nullable=False)
    nas_identifier = Column(String(255), unique=True, nullable=False)
    nas_secret = Column(Text, nullable=False)
    nas_secret_plain = Column(String(255), nullable=True)
    is_active = Column(Boolean, server_default="true")
    is_online = Column(Boolean, server_default="false", nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)

    site = relationship("Site", back_populates="routers")
    sessions = relationship("Session", back_populates="router")
    credentials = relationship("RouterCredential", back_populates="router", uselist=False)
    provision_logs = relationship("RouterProvisionLog", back_populates="router")
    metrics = relationship("RouterMetric", back_populates="router")


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
        ENUM("provision", "update_radius", "update_hotspot", "apply_template", "reboot", "diagnostics", name="router_provision_action", create_type=False),
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
